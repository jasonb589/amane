"""Amazon 源里的纯函数: 结果解析 / 标题匹配 / 海报地址."""

from amane.crawlers.sites.amazon import _parse_results, _poster_urls, _title_matches


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


def test_poster_urls_prefers_original_then_scaled() -> None:
    urls = _poster_urls("81abc")
    assert urls[0] == "https://m.media-amazon.com/images/I/81abc.jpg"
    assert urls[1] == "https://m.media-amazon.com/images/I/81abc._SL1500_.jpg"
