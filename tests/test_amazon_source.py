"""Amazon 源里的纯函数: 结果解析 / 标题匹配 / 海报地址, 以及年龄门重试."""

from __future__ import annotations

import asyncio
from html import escape
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from amane.aggregate.models import AggregatedMetadata
from amane.config import SiteConfig
from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.amazon import (
    _GATE_RE,
    AmazonCrawler,
    _clean,
    _image_from_detail,
    _keywords,
    _match_score,
    _parse_results,
    _poster_urls,
    _search_title,
    _title_matches,
)
from amane.net.errors import FailureKind, FailureReason, RequestError, RequestFailure, SourceError
from amane.parsing import ContentType


def test_profile_leaves_accept_language_to_fingerprint() -> None:
    """自定义 ``ja-JP`` 会让亚马逊返回 /age_check/ 年龄门, 必须交给 curl_cffi 指纹自带."""
    assert "Accept-Language" not in AmazonCrawler.profile().headers


def test_parse_results_reads_asin_image_and_alt() -> None:
    html = (
        '<div data-asin="B00TEST001"><img alt="Diabolus 鬼哭 下巻 DVD" '
        'src="https://m.media-amazon.com/images/I/81abc+-XYZ._AC_.jpg"></div><div data-asin="B00TEST002"><img '
        'alt="別の商品" src="https://m.media-amazon.com/images/I/71zzzQ.jpg"></div>'
    )
    out = _parse_results(html)
    assert out[0] == ("B00TEST001", "81abc+-XYZ", "Diabolus 鬼哭 下巻 DVD")
    assert out[1][0] == "B00TEST002"
    assert len(out) == 2


def test_parse_results_skips_blocks_without_image() -> None:
    html = (
        '<div data-asin="B00TEST003"><span>没有图片的块</span></div><div data-asin="B00TEST004"><img alt="有图有真相的商品" '
        'src="https://m.media-amazon.com/images/I/71zzzQ.jpg"></div>'
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
        "IPZZ-034",
    ]


def test_keywords_use_full_budget_without_number() -> None:
    assert _keywords("禁欲の果て 3日間 相沢みなみ アイデアポケット [DVD]", "") == [
        "禁欲の果て 3日間 相沢みなみ アイデアポケット",
        "禁欲の果て 3日間 相沢みなみ",
        "禁欲の果て 3日間",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("義母（前編）", "義母 前編"),
        ("【作品の原題】主演名 [DVD]", "作品の原題 主演名"),
        ("彼女との秘密（第2巻）", "彼女との秘密 第2巻"),
        ("【限定特典あり】彼女との秘密 [DVD]", "彼女との秘密"),
        ("【視聴期限なし】彼女との秘密（後編） [Blu-ray]", "彼女との秘密 後編"),
    ],
)
def test_clean_preserves_meaningful_bracketed_title(raw: str, expected: str) -> None:
    assert _clean(raw) == expected


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


@pytest.mark.parametrize(
    ("title", "number", "expected"),
    [
        ("彼女との秘密", "ABC-001", ["彼女との秘密", "ABC-001"]),
        ("", "ABC-001", ["ABC-001"]),
        ("", "", []),
        ("", "NOT A NUMBER", []),
        ("", "12345", []),
        ("彼女との秘密 女優名", "ABC-001", ["彼女との秘密 女優名", "彼女との秘密", "ABC-001"]),
    ],
)
def test_keywords_include_single_word_titles(title: str, number: str, expected: list[str]) -> None:
    assert _keywords(title, number) == expected


def test_match_score_prefers_title_hit_over_number_only() -> None:
    wanted = "義母 前編"
    titled = _match_score("JUFD-001", wanted, "義母 前編 アイデアポケット [DVD]")
    number_only = _match_score("JUFD-001", wanted, "JUFD-001 アイデアポケット [DVD]")
    assert titled > number_only > 0


def test_match_score_rejects_number_hit_on_goods_or_non_disc_listing() -> None:
    assert _match_score("IPZZ-034", "禁欲の果て", "IPZZ-034 桜空もも 特典生写真") == 0
    assert _match_score("IPZZ-034", "禁欲の果て", "IPZZ-034 桜空もも") == 0


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        ("", "ABC-0010 [DVD]"),
        ("", "XABC-001 [DVD]"),
        ("彼女との秘密", "彼女との秘密 ABC-0010 [DVD]"),
        ("彼女との秘密", "彼女との秘密 ABC-002 [DVD]"),
        ("彼女との秘密", "彼女との秘密 abc002 [DVD]"),
        ("彼女との秘密 前編", "彼女との秘密 後編 [DVD]"),
        ("彼女との秘密 上巻", "彼女との秘密 下巻 [DVD]"),
        ("彼女との秘密 第1巻", "彼女との秘密 第2巻 [DVD]"),
        ("彼女との秘密 前編 第1巻", "彼女との秘密 後編 第1巻 [DVD]"),
        ("彼女との秘密 前編 第1巻", "彼女との秘密 前編 第2巻 [DVD]"),
        ("彼女との秘密 上巻 第1話", "彼女との秘密 下巻 第1話 [DVD]"),
        ("彼女との秘密 前編", "彼女との秘密 後編 ABC-001 [DVD]"),
        ("彼女との秘密 第1巻", "彼女との秘密 第2巻 ABC-001 [DVD]"),
        ("彼女との秘密", "彼女との秘密 ポストカード"),
        ("彼女との秘密", "彼女との秘密 特典生写真"),
        ("", "ABC-001 ポストカード"),
        ("", "ABC-001 桜空もも"),
    ],
)
def test_match_score_rejects_conflicting_products(wanted: str, found: str) -> None:
    assert _match_score("ABC-001", wanted, found) == 0


@pytest.mark.parametrize(
    ("wanted", "found"),
    [
        ("", "ABC-001 [DVD]"),
        ("", "abc001 [DVD]"),
        ("", "ＡＢＣ－００１ [DVD]"),
        ("彼女との秘密", "彼女との秘密 [DVD] 特典ポストカード付き"),
        ("彼女との秘密 前編", "彼女との秘密 前編 [DVD]"),
        ("彼女との秘密 第2巻", "彼女との秘密 第２巻 [DVD]"),
        ("彼女との秘密 前編 第2巻", "彼女との秘密 前編 第２巻 [DVD]"),
    ],
)
def test_match_score_keeps_compatible_disc_products(wanted: str, found: str) -> None:
    assert _match_score("ABC-001", wanted, found) > 0


def _card(asin: str, title: str, image_id: str = "81HITJP") -> str:
    """生成独立的合成商品卡片, 用于解析边界测试."""
    return (
        f'<div data-asin="{asin}"><img class="s-image" alt="{escape(title, quote=True)}" '
        f'src="https://m.media-amazon.com/images/I/{image_id}._AC_UL320_.jpg"></div>'
    )


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


@pytest.mark.parametrize(
    "page",
    [
        (
            "<div data-asin='B00TEST001'><img class='s-image' alt='彼女との秘密 [DVD]' "
            "src='https://m.media-amazon.com/images/I/81HITJP.jpg'></div>"
        ),
        (
            '<div data-asin="B00TEST001"><h2><span>彼女との秘密 [DVD]</span></h2>'
            '<img class="s-image" alt="" src="https://m.media-amazon.com/images/I/81HITJP.jpg"></div>'
        ),
        (
            '<div data-asin="B00TEST001"><img alt="Amazonおすすめ" '
            'src="https://m.media-amazon.com/images/I/71BADGE.jpg"><h2>彼女との秘密 [DVD]</h2>'
            '<img class="s-image" alt="彼女との秘密 [DVD]" '
            'src="https://m.media-amazon.com/images/I/81HITJP.jpg"></div>'
        ),
        (
            '<div data-asin="B00TEST001"><h2>彼女との秘密 [DVD]</h2>'
            '<img class="s-image" src="https://m.media-amazon.com/images/I/01RmK+J4pJL.gif" '
            'data-src="https://m.media-amazon.com/images/I/81HITJP.jpg"></div>'
        ),
        (
            '<div data-asin="B00TEST001"><h2>彼女との秘密 [DVD]</h2>'
            '<img class="s-image" src="https://m.media-amazon.com/images/I/01RmK+J4pJL.gif" '
            'srcset="https://m.media-amazon.com/images/I/81HITJP._AC_UL320_.jpg 1x, '
            'https://m.media-amazon.com/images/I/81HITJP._AC_UL640_.jpg 2x"></div>'
        ),
    ],
    ids=["single-quotes", "heading", "badge-before-cover", "lazy-data-src", "lazy-srcset"],
)
@pytest.mark.asyncio
async def test_fetch_handles_card_layouts_with_one_request(page: str) -> None:
    client = _StubClient([page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("ABC-001", partial_result=AggregatedMetadata("ABC-001", title="彼女との秘密"))

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST001"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/81HITJP.jpg"
    assert len(client.calls) == 1


def test_parse_results_preserves_long_product_title() -> None:
    title = "彼女との秘密" + "ふたりだけの物語" * 30 + " [DVD]"
    assert _parse_results(_card("B00TEST001", title)) == [("B00TEST001", "81HITJP", title)]


def test_parse_results_keeps_title_without_adopting_unrelated_only_image() -> None:
    page = (
        '<div data-asin="B00TEST001"><h2>彼女との秘密 [DVD]</h2>'
        '<img alt="Amazonおすすめ" src="https://m.media-amazon.com/images/I/71BADGE.jpg"></div>'
    )
    assert _parse_results(page) == [("B00TEST001", "", "彼女との秘密 [DVD]")]


def test_parse_results_keeps_nested_product_images_with_their_asin() -> None:
    page = (
        '<div data-asin="B00TEST001"><h2>彼女との秘密 [DVD]</h2>'
        '<div data-asin="B00TEST002"><h2>別の商品 [DVD]</h2>'
        '<img class="s-image" src="https://m.media-amazon.com/images/I/81OTHER.jpg"></div></div>'
    )
    assert _parse_results(page) == [
        ("B00TEST001", "", "彼女との秘密 [DVD]"),
        ("B00TEST002", "81OTHER", "別の商品 [DVD]"),
    ]


@pytest.mark.parametrize("placeholder_first", [True, False])
@pytest.mark.asyncio
async def test_fetch_merges_duplicate_asin_without_losing_cover(placeholder_first: bool) -> None:
    cards = [
        _card("B00TEST001", "彼女との秘密 [DVD]", "01RmK+J4pJL"),
        _card("B00TEST001", "彼女との秘密 [DVD]"),
    ]
    if not placeholder_first:
        cards.reverse()
    page = "".join(cards)
    assert _parse_results(page) == [("B00TEST001", "81HITJP", "彼女との秘密 [DVD]")]
    client = _StubClient([page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("ABC-001", partial_result=AggregatedMetadata("ABC-001", title="彼女との秘密"))

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/81HITJP.jpg"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_fetch_scores_results_after_first_eight_cards() -> None:
    page = "".join(_card(f"B00OTHER{i:02d}", f"無関係の商品{i} [DVD]", "71OTHER") for i in range(8))
    page += _card("B00TEST009", "彼女との秘密 [DVD]")
    client = _StubClient([page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("ABC-001", partial_result=AggregatedMetadata("ABC-001", title="彼女との秘密"))

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST009"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_fetch_searches_number_when_prior_sources_have_no_title() -> None:
    client = _StubClient([_card("B00TEST001", "ABC-001 [DVD]")])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]

    meta = await crawler.fetch(SearchQuery("ABC-001"))

    assert meta is not None
    assert meta.external_id == "B00TEST001"
    assert len(client.calls) == 1
    assert parse_qs(urlsplit(client.calls[0]).query)["k"] == ["ABC-001"]


@pytest.mark.parametrize("number", ["", "NOT A NUMBER", "RANDOMTITLE", "12345"])
@pytest.mark.asyncio
async def test_fetch_skips_invalid_number_without_title(number: str) -> None:
    client = _StubClient([])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]

    assert await crawler.fetch(SearchQuery(number)) is None
    assert not client.calls


@pytest.mark.parametrize(
    ("title", "expected_terms"),
    [
        (
            "禁欲の果て 3日間 相沢みなみ アイデアポケット",
            ["禁欲の果て 3日間 相沢みなみ アイデアポケット", "禁欲の果て 3日間 相沢みなみ", "ABC-001"],
        ),
        (None, ["ABC-001"]),
    ],
)
@pytest.mark.asyncio
async def test_fetch_keeps_search_requests_within_keyword_budget(title: str | None, expected_terms: list[str]) -> None:
    client = _StubClient(["<html><body>no results</body></html>"] * (2 * len(expected_terms)))
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    partial = AggregatedMetadata("ABC-001", title=title) if title else None

    with pytest.raises(SourceError) as error:
        await crawler.fetch(SearchQuery("ABC-001", partial_result=partial))

    assert error.value.reason is FailureReason.NOT_LISTED
    assert len(client.calls) == 2 * len(expected_terms)
    assert [parse_qs(urlsplit(url).query)["k"][0] for url in client.calls] == [
        term for term in expected_terms for _ in range(2)
    ]


@pytest.mark.asyncio
async def test_fetch_retries_when_age_gate_blocks() -> None:
    """真正的 /age_check/ 是概率性的: 换一次请求就正常, 不该直接把整源判失败."""
    gate = '<form name="agecheck" action="/age_check/"><input class="driver-verify"></form>'
    page = (
        '<div data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/81ABCDE.jpg"></div>'
    )
    client = _StubClient([gate, page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="BEAUTY VENUS VIII")

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
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="BEAUTY VENUS VIII")

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
    miss = '<div data-asin="B00OTHER01"><img alt="別の作品 [DVD]" src="https://m.media-amazon.com/images/I/71other.jpg"></div>'
    hit = (
        '<div data-asin="B00TEST009"><img alt="義母 前編 アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/81HITJP.jpg"></div>'
    )
    client = _StubClient([miss, miss, hit])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("JUFD-001", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(
        number="IPZZ-034", title="【視聴期限なし】義母 前編 アイデアポケット [DVD]"
    )

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST009"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/81HITJP.jpg"
    assert len(client.calls) == 3
    terms = [parse_qs(urlsplit(url).query)["k"][0] for url in client.calls]
    assert terms[0] == terms[1]
    assert terms[2] != terms[0]


def test_parse_results_keeps_placeholder_candidate_without_image() -> None:
    """成人卡片未加载时只给 60x40 占位图: 候选要留着 (靠标题命中), 图片留给详情页补."""
    html = (
        '<div data-asin="B00ADULT01"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif"></div><div '
        'data-asin="B00OTHER02"><img alt="別の商品 [DVD]" src="https://m.media-amazon.com/images/I/71zzzQQ.jpg"></div>'
    )
    out = _parse_results(html)
    assert [(row[0], row[1]) for row in out] == [("B00ADULT01", ""), ("B00OTHER02", "71zzzQQ")]


@pytest.mark.asyncio
async def test_fetch_reads_cover_from_detail_page_when_search_shows_placeholder() -> None:
    """结果页只给占位图时, 用 ASIN 去详情页拿 data-old-hires 的真封面."""
    search = (
        '<div data-asin="B00ADULT09"><img alt="義母 前編 アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif"></div>'
    )
    detail = (
        '<img id="landingImage" src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif" '
        'data-old-hires="https://m.media-amazon.com/images/I/816fsRcEijL._AC_SL1500_.jpg">'
    )
    client = _StubClient([search, detail])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("JUFD-001", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="義母 前編 アイデアポケット [DVD]")

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00ADULT09"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/816fsRcEijL.jpg"
    assert client.calls[1] == "https://www.amazon.co.jp/dp/B00ADULT09"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_asin_main_image() -> None:
    """详情页回 JS 空壳 (200 但无原图) 时, 退到 /images/P/<ASIN>.jpg —— 实测 351x500 总是有货."""
    search = (
        '<div data-asin="B00ADULT10"><img alt="義母 前編 アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif"></div>'
    )
    shell = "<html><body><script>var hiRes=1</script>no product image</body></html>"
    client = _StubClient([search, shell, shell, shell])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("JUFD-001", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="義母 前編 アイデアポケット [DVD]")

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00ADULT10"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/P/B00ADULT10.jpg"
    # 小图之后补一条按 ASIN 镜像日亚封面的地址 (1055x1500), 由管线按面积取最大.
    assert meta.poster_urls[-1] == "https://tenhow.net/images/B00ADULT10.jpg"


@pytest.mark.asyncio
async def test_fetch_retries_other_detail_entries_when_page_is_shell() -> None:
    """详情页回 JS 空壳时换入口重试, 命中原图就不该退到 351x500 的小图."""
    search = (
        '<div data-asin="B00ADULT11"><img alt="義母 前編 アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif"></div>'
    )
    shell = "<html><body>no product image</body></html>"
    full = '<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/816fsRcEijL._AC_SL1500_.jpg">'
    client = _StubClient([search, shell, full])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("JUFD-001", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="義母 前編 アイデアポケット [DVD]")

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/816fsRcEijL.jpg"
    assert client.calls[1] == "https://www.amazon.co.jp/dp/B00ADULT11"
    assert client.calls[2] == "https://www.amazon.co.jp/dp/B00ADULT11?th=1&psc=1"


_MOBILE_MISS = (
    '<div data-asin="B00MOBILE1"><img alt="別の作品 [DVD]" '
    'src="https://m.media-amazon.com/images/I/71mobile.jpg"></div>'
)
"""移动版页里只有一条无关条目: 用来把流程推到桌面版 (黑幕那块)."""


@pytest.mark.asyncio
async def test_fetch_prefers_mobile_search_page() -> None:
    """移动版结果页直接给真封面 (没有黑幕占位图): 一个请求搞定, 不该再走桌面版."""
    mobile = (
        '<div data-asin="B00MOB0001"><img alt="義母 前編 成咲優美" '
        'src="https://m.media-amazon.com/images/I/81WvjXYyCrL._AC_UL320_.jpg"></div>'
    )
    client = _StubClient([mobile])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("義母 前編", content_type=ContentType.HENTAI)

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00MOB0001"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/81WvjXYyCrL.jpg"
    assert "/gp/aw/s?" in client.calls[0]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_fetch_unlocks_black_curtain_for_adult_covers() -> None:
    """移动版没命中时回桌面版: 看到黑幕就点一次「Yes」, 跳回来的正文才有真封面."""
    curtain = (
        '<span class="s-black-curtain-modal-content">Age Verification</span><div data-asin="B00CURTAIN"><img '
        'alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif"></div>'
    )
    unlocked = (
        '<div data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/91bhG1CxHUL._AC_UL320_.jpg"></div>'
    )
    client = _StubClient([_MOBILE_MISS, curtain, "<html>ok</html>", unlocked])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="BEAUTY VENUS VIII")

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST001"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/91bhG1CxHUL.jpg"
    assert "/gp/aw/s?" in client.calls[0]
    assert "/black-curtain/save-eligibility/" in client.calls[2]
    assert len(client.calls) == 4


@pytest.mark.asyncio
async def test_fetch_uses_unlock_response_when_it_already_has_covers() -> None:
    """点「Yes」跳回来的那一页本身就带真封面: 直接吃掉, 不该再多发一次搜索请求."""
    curtain = (
        '<span class="s-black-curtain"></span><div data-asin="B00CURTAIN"><img alt="BEAUTY VENUS Ⅷ [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif"></div>'
    )
    unlocked = (
        '<div data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/91bhG1CxHUL._AC_UL320_.jpg"></div>'
    )
    client = _StubClient([_MOBILE_MISS, curtain, unlocked])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="BEAUTY VENUS VIII")

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST001"
    assert "/black-curtain/save-eligibility/" in client.calls[2]
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_fetch_unlocks_black_curtain_only_once() -> None:
    """黑幕只点一次: 换检索词时不该每次都去打那个保存接口."""
    curtain = (
        '<span class="s-black-curtain"></span><div data-asin="B00CURTAIN"><img alt="べつの作品 [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif"></div>'
    )
    hit = (
        '<div data-asin="B00TEST077"><img alt="義母 前編 アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/91ZYXW9HUL._AC_UL320_.jpg"></div>'
    )
    # 检索词1: 移动版无关 → 桌面版黑幕 → 解锁(正文没命中) → 重取还是黑幕; 检索词2: 移动版命中
    client = _StubClient([_MOBILE_MISS, curtain, "<html>ok</html>", curtain, hit])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("JUFD-001", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(number="IPZZ-034", title="義母 前編 アイデアポケット")

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST077"
    assert sum(1 for call in client.calls if "save-eligibility" in call) == 1


def test_search_title_prefers_raw_japanese_title() -> None:
    """字段语言是中文时聚合标题是译名, 检索必须换成来源里的日文原题."""
    partial = AggregatedMetadata(
        number="IPZZ-034",
        title="义母 前篇",
        raw={"r18dev:jp": {"title": "義母 前編"}, "dmm": {"title": "義母 前編 [DVD]"}},
    )
    assert _search_title(partial) == "義母 前編"


def test_search_title_falls_back_to_aggregated_title() -> None:
    assert (
        _search_title(AggregatedMetadata(number="IPZZ-034", title="BEAUTY VENUS VIII", raw={})) == "BEAUTY VENUS VIII"
    )
    assert _search_title(None) == ""


def test_search_title_uses_japanese_number_as_title() -> None:
    """里番没有番号: 文件名里的日文标题就落在 number 上, 所有来源都失手时也拿它检索."""
    assert _search_title(None, "義母 前編") == "義母 前編"
    assert _search_title(None, "IPZZ-034") == ""


@pytest.mark.asyncio
async def test_fetch_searches_japanese_title_when_field_is_translated() -> None:
    page = (
        '<div data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/81ABCDE.jpg"></div>'
    )
    client = _StubClient([page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = AggregatedMetadata(
        number="IPZZ-034",
        title="美颜维纳斯 第8集",
        raw={"r18dev:jp": {"title": "BEAUTY VENUS VIII"}},
    )

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST001"
    assert "BEAUTY%20VENUS%20VIII" in client.calls[0]


@pytest.mark.parametrize("attribute", ["data-old-hires", "src"])
def test_detail_placeholder_does_not_override_real_dynamic_image(attribute: str) -> None:
    html = (
        f'<img id="landingImage" {attribute}="https://m.media-amazon.com/images/I/01RmK+J4pJL.gif" '
        'data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/81ABCDE.jpg&quot;:[1000,1500]}">'
    )
    assert _image_from_detail(html) == "81ABCDE"


@pytest.mark.asyncio
async def test_search_failures_preserve_network_reason() -> None:
    failure = RequestError("https://www.amazon.co.jp/s", RequestFailure(FailureKind.TIMEOUT, "timeout"))
    client = _StubClient([failure] * 4)
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("ABC-001", partial_result=AggregatedMetadata("ABC-001", title="彼女との秘密"))
    with pytest.raises(RequestError) as error:
        await crawler.fetch(query)
    assert error.value.reason is FailureReason.TIMEOUT
    assert len(client.calls) == 4


@pytest.mark.asyncio
async def test_concurrent_fetches_keep_failure_state_separate() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class ConcurrentClient:
        async def get_text(self, url: str, **kwargs: Any) -> str:
            if "FAIL" in url:
                entered.set()
                await release.wait()
                raise RequestError(url, RequestFailure(FailureKind.TIMEOUT, "timeout"))
            return "<html>no results</html>"

    crawler = AmazonCrawler(ConcurrentClient())  # type: ignore[arg-type]
    failed = SearchQuery("FAIL-001", partial_result=AggregatedMetadata("FAIL-001", title="FAIL TITLE"))
    missing = SearchQuery("MISS-001", partial_result=AggregatedMetadata("MISS-001", title="MISSING TITLE"))
    task = asyncio.create_task(crawler.fetch(failed))
    await entered.wait()
    try:
        with pytest.raises(SourceError) as error:
            await crawler.fetch(missing)
        assert error.value.reason is FailureReason.NOT_LISTED
    finally:
        release.set()
    with pytest.raises(RequestError) as error:
        await task
    assert error.value.reason is FailureReason.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        None,
        "https://mirror.example",
        "https://mirror.example/",
        "http://mirror.example:8080/amazon/jp/",
        "https://mirror.example:443/amazon%20jp/",
    ],
)
async def test_configured_base_covers_complete_page_request_chain(base_url: str | None) -> None:
    gate = '<form name="agecheck" action="/age_check/"></form>'
    placeholder = _card("B00TEST001", "ABC-001 [DVD]", "01RmK+J4pJL")
    curtain = '<span class="s-black-curtain"></span>' + placeholder
    detail = '<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/81HITJP.jpg">'
    shell = '<a href="https://third-party.example/dp/B00TEST001">Other page</a>'
    web = AsyncMock()
    web.get_text.side_effect = [gate, _MOBILE_MISS, curtain, "<html>ok</html>", placeholder, shell, shell, detail]
    cookies = {"session": "configured"}
    crawler = AmazonCrawler(HttpClient(web=web), SiteConfig(base_url=base_url, cookie=cookies))

    result = await crawler.fetch(SearchQuery("ABC-001"))

    base = (base_url or "https://www.amazon.co.jp").rstrip("/")
    mobile = f"{base}/gp/aw/s?k=ABC-001&i=dvd"
    desktop = f"{base}/s?k=ABC-001&i=dvd"
    requests = [call.args[0] for call in web.get_text.await_args_list]
    assert requests[:3] == [mobile, mobile, desktop]
    eligibility = urlsplit(requests[3])
    assert eligibility.path == urlsplit(base).path + "/-/en/black-curtain/save-eligibility/black-curtain"
    assert parse_qs(eligibility.query) == {"returnUrl": [desktop]}
    assert requests[4:] == [
        desktop,
        f"{base}/dp/B00TEST001",
        f"{base}/dp/B00TEST001?th=1&psc=1",
        f"{base}/-/en/dp/B00TEST001",
    ]
    assert all(urlsplit(url).netloc == urlsplit(base).netloc for url in requests)
    for call in web.get_text.await_args_list:
        assert call.kwargs == {"cookies": cookies, "headers": None, "encoding": "utf-8"}
    assert result is not None
    assert result.source_url == f"{base}/dp/B00TEST001"
    assert result.poster_urls == [
        "https://m.media-amazon.com/images/I/81HITJP.jpg",
        "https://m.media-amazon.com/images/I/81HITJP._SL1500_.jpg",
    ]


@pytest.mark.asyncio
async def test_mirror_keeps_original_cdn_fallbacks_when_details_have_no_image() -> None:
    base = "https://mirror.example/amazon"
    web = AsyncMock()
    web.get_text.side_effect = [_card("B00TEST001", "ABC-001 [DVD]", "01RmK+J4pJL"), "shell", "shell", "shell"]
    crawler = AmazonCrawler(HttpClient(web=web), SiteConfig(base_url=base))

    result = await crawler.fetch(SearchQuery("ABC-001"))

    assert result is not None and result.source_url == f"{base}/dp/B00TEST001"
    assert result.poster_urls == [
        "https://m.media-amazon.com/images/P/B00TEST001.jpg",
        "https://m.media-amazon.com/images/P/B00TEST001.01._SCLZZZZZZZ_.jpg",
        "https://tenhow.net/images/B00TEST001.jpg",
    ]
    assert all(call.args[0].startswith(f"{base}/") for call in web.get_text.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["page", "eligibility"])
@pytest.mark.parametrize(
    "url",
    [
        "https://third-party.example/amazon/dp/B00TEST001",
        "https://mirror.example.evil.example/amazon/dp/B00TEST001",
        "https://mirror.example@third-party.example/amazon/dp/B00TEST001",
        "https://user:secret@mirror.example/amazon/dp/B00TEST001",
        "https://mirror.example:444/amazon/dp/B00TEST001",
        "https://mirror.example:0/amazon/dp/B00TEST001",
        "http://mirror.example/amazon/dp/B00TEST001",
        "ftp://mirror.example/amazon/dp/B00TEST001",
        "https://mirror.example/amazon-other/dp/B00TEST001",
        "https://mirror.example/dp/B00TEST001",
        "https://mirror.example/amazon/../other",
        "https://mirror.example/amazon/%2e%2e/other",
        "https://mirror.example/amazon/%5cother",
        "https://www.amazon.co.jp/dp/B00TEST001",
        "https://[invalid/amazon/dp/B00TEST001",
        "//mirror.example/amazon/dp/B00TEST001",
        "/amazon/dp/B00TEST001",
    ],
)
async def test_page_and_eligibility_reject_urls_outside_configured_base(operation: str, url: str) -> None:
    web = AsyncMock()
    crawler = AmazonCrawler(
        HttpClient(web=web), SiteConfig(base_url="https://mirror.example/amazon", cookie={"session": "configured"})
    )

    with pytest.raises(SourceError) as error:
        if operation == "page":
            await crawler._fetch_page(url)
        else:
            await crawler._unlock(url)

    assert error.value.reason is FailureReason.PARSE_ERROR
    web.get_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://mirror.example/amazon",
        "https://user:secret@mirror.example/amazon",
        "https://mirror.example/amazon?token=private",
        "https://mirror.example/amazon#anchor",
        "https://mirror.example:invalid/amazon",
        "https://[invalid/amazon",
        "https://mirror.example/amazon/../other",
        "https://mirror.example\\@third-party.example/amazon",
        "mirror.example/amazon",
    ],
)
async def test_invalid_base_does_not_fall_back_to_official_site(base_url: str) -> None:
    web = AsyncMock()
    crawler = AmazonCrawler(HttpClient(web=web), SiteConfig(base_url=base_url, cookie={"session": "configured"}))

    with pytest.raises(SourceError) as error:
        await crawler.fetch(SearchQuery("ABC-001"))

    assert error.value.reason is FailureReason.PARSE_ERROR
    assert error.value.detail == "Amazon 页面地址不属于配置站点"
    web.get_text.assert_not_awaited()
