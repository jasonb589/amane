"""里番「串集」防护: 标题里的数字必须一致（＃1 vs ＃4）。"""

from amane.crawlers.sites.dmm import _hentai_digits


def test_different_episode_numbers_differ() -> None:
    assert _hentai_digits("OVA初恋時間。 ＃1 セル版") != _hentai_digits("OVA初恋時間。 ＃4 セル版")


def test_same_episode_with_spacing_is_equal() -> None:
    assert _hentai_digits("OVA初恋時間。 ＃1 セル版") == _hentai_digits("OVA初恋時間。＃1 セル版")


def test_fullwidth_digits_are_normalized() -> None:
    assert _hentai_digits("第１話") == _hentai_digits("第1話")


def test_no_digits_gives_empty_set() -> None:
    assert _hentai_digits("眠れないオンナ") == frozenset()
