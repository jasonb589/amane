"""里番候选排序: 配信页（video.dmm.co.jp）优先于通販页。"""

from amane.crawlers.sites.dmm import _hentai_priority


def test_digital_comes_first() -> None:
    digital = "https://www.dmm.co.jp/digital/anime/-/detail/=/cid=h_1a/"
    mono = "https://www.dmm.co.jp/mono/anime/-/detail/=/cid=196glod0313/"
    assert _hentai_priority(digital) < _hentai_priority(mono)


def test_mono_before_others() -> None:
    mono = "https://www.dmm.co.jp/mono/anime/-/detail/=/cid=196glod0313/"
    other = "https://www.dmm.co.jp/rental/-/detail/=/cid=x/"
    assert _hentai_priority(mono) < _hentai_priority(other)


def test_sort_is_stable_within_tier() -> None:
    mono1 = "https://www.dmm.co.jp/mono/anime/-/detail/=/cid=aaa/"
    mono2 = "https://www.dmm.co.jp/mono/anime/-/detail/=/cid=bbb/"
    digital = "https://www.dmm.co.jp/digital/anime/-/detail/=/cid=ccc/"
    ordered = sorted([mono1, mono2, digital], key=_hentai_priority)
    assert ordered == [digital, mono1, mono2]
