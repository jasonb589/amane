"""mgstage 图片尺寸推导: 展开大图 pb_e_ (840x473) 与竖版海报 pf_e_ (422x600)."""

import pytest

from amane.crawlers.sites.mgstage import _poster_url, _wide_cover

BASE = "https://image.mgstage.com/images/shirouto/siro/5755"


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        # 小横版 540x304 与 小竖版 166x236 都指向同一张图的展开大图.
        ("pb_p", "pb_e"),
        ("pf_o1", "pb_e"),
        ("pf_o2", "pb_e"),
        # 已经是展开大图 / 根本不是封面前缀 → 推不出.
        ("pb_e", None),
        ("cap_e_0", None),
    ],
)
def test_wide_cover(prefix: str, expected: str | None) -> None:
    url = f"{BASE}/{prefix}_siro-5755.jpg"
    wide = _wide_cover(url)
    if expected is None:
        assert wide is None
    else:
        assert wide == f"{BASE}/{expected}_siro-5755.jpg"


def test_wide_cover_empty() -> None:
    assert _wide_cover("") is None


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("pb_e", "pf_e"),  # 横版展开图 → 同目录竖版海报
        ("pb_p", None),
        ("pf_o1", None),
    ],
)
def test_poster_url(prefix: str, expected: str | None) -> None:
    url = f"{BASE}/{prefix}_siro-5755.jpg"
    poster = _poster_url(url)
    if expected is None:
        assert poster is None
    else:
        assert poster == f"{BASE}/{expected}_siro-5755.jpg"
