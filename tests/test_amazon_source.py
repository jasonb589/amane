"""Amazon 源里的纯函数: 结果解析 / 标题匹配 / 海报地址, 以及年龄门重试."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.amazon import AmazonCrawler, _parse_results, _poster_urls, _title_matches
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


class _StubClient:
    """按顺序应答: 元素为异常时抛出, 否则当页面正文."""

    def __init__(self, items: list[object]) -> None:
        self._items = list(items)
        self.calls: list[str] = []

    async def get_html(self, url: str, **kwargs: Any) -> str:
        del kwargs
        self.calls.append(url)
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return str(item)


@pytest.mark.asyncio
async def test_fetch_retries_when_age_gate_blocks() -> None:
    """亚马逊偶发返回 /age_check/: 换一次请求就正常, 不该直接把整源判失败."""
    page = (
        'data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/81abc.jpg">'
    )
    client = _StubClient([SourceError(FailureReason.AGE_VERIFICATION), page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = SimpleNamespace(title="BEAUTY VENUS VIII")  # type: ignore[assignment]

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.poster_urls
    assert len(client.calls) == 2
