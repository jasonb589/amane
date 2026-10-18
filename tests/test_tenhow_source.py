"""Tenhow 源里的纯函数: 番号归一化 / cid→ASIN 映射 / 演员名索引."""

from amane.crawlers.sites.tenhow import _cid_map, _keys, _names_on_page, _norm_name


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
