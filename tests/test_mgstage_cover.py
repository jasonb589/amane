"""mgstage 图片地址推导: 按目录拼 pb_e_(封面) / pf_e_(海报) / cap_e_N_(剧照)."""

import pytest

from amane.crawlers.sites.mgstage import capture_count, image_dir, image_urls

_SIRO = """
<a id="EnlargeImage" class="link_magnify" href="https://image.mgstage.com/images/shirouto/siro/5755/pb_e_siro-5755.jpg"></a>
<img class="enlarge_image" src="https://image.mgstage.com/images/shirouto/siro/5755/pf_o1_siro-5755.jpg">
<a class="sample_image" href="https://image.mgstage.com/images/shirouto/siro/5755/cap_e_0_siro-5755.jpg"></a>
<img src="https://image.mgstage.com/images/shirouto/siro/5755/cap_t1_0_siro-5755.jpg">
<a class="sample_image" href="https://image.mgstage.com/images/shirouto/siro/5755/cap_e_1_siro-5755.jpg"></a>
<a class="sample_image" href="https://image.mgstage.com/images/shirouto/siro/5755/cap_e_2_siro-5755.jpg"></a>
"""

# 主图是小横版 pb_p_ (540x304) 的模板: 以前正是这种被漏掉, 背景图停在 540x304.
_GANA = """
<img class="enlarge_image" src="https://image.mgstage.com/images/nanpatv/200gana/3470/pb_p_200gana-3470.jpg">
<a class="sample_image" href="https://image.mgstage.com/images/nanpatv/200gana/3470/cap_e_0_200gana-3470.jpg"></a>
"""

BASE_SIRO = "https://image.mgstage.com/images/shirouto/siro/5755/"
BASE_GANA = "https://image.mgstage.com/images/nanpatv/200gana/3470/"


def test_image_dir_from_any_prefix() -> None:
    assert image_dir(_SIRO) == (BASE_SIRO, "siro-5755")
    assert image_dir(_GANA) == (BASE_GANA, "200gana-3470")


def test_image_dir_without_images() -> None:
    assert image_dir("<html>no images</html>") is None


@pytest.mark.parametrize(("page", "expected"), [(_SIRO, 3), (_GANA, 1), ("<html></html>", 0)])
def test_capture_count(page: str, expected: int) -> None:
    assert capture_count(page) == expected


def test_image_urls_build_all_sizes() -> None:
    thumbs, posters, fanart = image_urls(_SIRO)
    assert thumbs == [f"{BASE_SIRO}pb_e_siro-5755.jpg"]
    assert posters == [f"{BASE_SIRO}pf_e_siro-5755.jpg"]
    assert fanart == [
        f"{BASE_SIRO}cap_e_0_siro-5755.jpg",
        f"{BASE_SIRO}cap_e_1_siro-5755.jpg",
        f"{BASE_SIRO}cap_e_2_siro-5755.jpg",
    ]


def test_image_urls_upgrade_small_cover_prefix() -> None:
    """主图是 pb_p_ 时也要给出 840x473 的 pb_e_ —— 这正是之前漏掉的那类模板."""
    thumbs, posters, fanart = image_urls(_GANA)
    assert thumbs == [f"{BASE_GANA}pb_e_200gana-3470.jpg"]
    assert posters == [f"{BASE_GANA}pf_e_200gana-3470.jpg"]
    assert fanart == [f"{BASE_GANA}cap_e_0_200gana-3470.jpg"]


def test_image_urls_empty() -> None:
    assert image_urls("<html>no images</html>") == ([], [], [])
