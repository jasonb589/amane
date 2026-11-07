"""Amazon 图片解析的合成页面回归: 主图归属、图库结构与地址校验."""

import json

import pytest

from amane.crawlers.sites.amazon import _image_from_detail, _image_id


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://m.media-amazon.com/images/I/81Abc+XYZ.jpg", "81Abc+XYZ"),
        ("https://m.media-amazon.com/images/I/81Abc%2BXYZ._AC_SL1500_.jpg", "81Abc+XYZ"),
        ("https://images-na.ssl-images-amazon.com/images/I/81AbcXYZ.jpg", "81AbcXYZ"),
        ("https://images-eu.ssl-images-amazon.com/images/I/81AbcXYZ._SL500_.jpg", "81AbcXYZ"),
        ("https://m.media-amazon.com/images/I/01RmK+J4pJL.gif", None),
        ("https://m.media-amazon.com/images/I/01RmK%2BJ4pJL._AC_UL80_.gif", None),
        ("https://m.media-amazon.com/images/I/01mI9NDJJTL.jpg", None),
        ("https://m.media-amazon.com.evil.example/images/I/81AbcXYZ.jpg", None),
        ("https://evil.example/m.media-amazon.com/images/I/81AbcXYZ.jpg", None),
        ("https://m.media-amazon.com@evil.example/images/I/81AbcXYZ.jpg", None),
        ("file://m.media-amazon.com/images/I/81AbcXYZ.jpg", None),
        ("javascript:https://m.media-amazon.com/images/I/81AbcXYZ.jpg", None),
        ("https://m.media-amazon.com/images/P/B00TEST001.jpg", None),
        ("https://m.media-amazon.com/images/I/81Abc%2FXYZ.jpg", None),
        ("https://m.media-amazon.com/images/I/81Abc%5CXYZ.jpg", None),
        ("https://m.media-amazon.com/images/I/81Abc%252FXYZ.jpg", None),
        ("https://m.media-amazon.com/images/I/81Abc%20XYZ.jpg", None),
        ("https://m.media-amazon.com/images/I/81AbcXYZ.jpg.exe.jpg", None),
        ("https://m.media-amazon.com/images/I/81AbcXYZ.bad.jpg", None),
        ("https://m.media-amazon.com/images/I/81AbcXYZ.\\wrong.jpg", None),
        ("https://m.media-amazon.com/images/I/81AbcXYZ.%5Cwrong.jpg", None),
        ("https://m.media-amazon.com/images/I/81AbcXYZ._AC%5CSL1500_.jpg", None),
        ("https://m.media-amazon.com/images/I/.jpg", None),
        ("https://[invalid/images/I/81AbcXYZ.jpg", None),
        ("", None),
    ],
)
def test_image_id_validates_origin_path_and_placeholder(url: str, expected: str | None) -> None:
    assert _image_id(url) == expected


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="https://m.media-amazon.com/images/I/81MAIN01.jpg" id="landingImage">',
        "<img id='landingImage' src='https://m.media-amazon.com/images/I/81MAIN01.jpg'>",
        '<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/81MAIN01.jpg">',
        "<img data-old-hires='https://m.media-amazon.com/images/I/81MAIN01.jpg' id='landingImage'>",
        '<img id="landingImage" data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/'
        '81MAIN01.jpg&quot;:[1000,1500]}">',
        "<img id='landingImage' data-a-dynamic-image='"
        '{"https://m.media-amazon.com/images/I/81MAIN01.jpg":[1000,1500]}\'>',
    ],
)
def test_detail_image_accepts_attribute_order_and_quote_variants(markup: str) -> None:
    assert _image_from_detail(markup) == "81MAIN01"


def test_detail_dynamic_image_selects_largest_valid_dimensions() -> None:
    markup = (
        "<img id='landingImage' data-a-dynamic-image='"
        '{"https://m.media-amazon.com/images/I/81SMALL1.jpg":[350,500],'
        '"https://m.media-amazon.com/images/I/81LARGE1.jpg":[1000,1500],'
        '"https://m.media-amazon.com/images/I/81TALL01.jpg":[100,2000],'
        '"https://m.media-amazon.com/images/I/01RmK%2BJ4pJL.gif":[2000,3000],'
        '"https://evil.example/images/I/81EVIL01.jpg":[3000,4000]}\'>'
    )
    assert _image_from_detail(markup) == "81LARGE1"


@pytest.mark.parametrize("dimensions", [None, [], [1000], [1000, "1500"], [0, 1500], [-1000, -1500]])
def test_detail_dynamic_image_rejects_invalid_dimensions(dimensions: object) -> None:
    dynamic = json.dumps(
        {
            "https://m.media-amazon.com/images/I/81INVALID.jpg": dimensions,
            "https://m.media-amazon.com/images/I/81MAIN01.jpg": [1000, 1500],
        }
    )
    markup = f"<img id='landingImage' data-a-dynamic-image='{dynamic}'>"
    assert _image_from_detail(markup) == "81MAIN01"


@pytest.mark.parametrize(
    ("main", "expected"),
    [
        ('<img id="landingImage" src="https://m.media-amazon.com/images/I/81MAIN01.jpg">', "81MAIN01"),
        ('<img id="landingImage" src="https://m.media-amazon.com/images/I/01RmK%2BJ4pJL.gif">', None),
        ("", None),
    ],
)
def test_detail_image_ignores_recommendation_images(main: str, expected: str | None) -> None:
    markup = (
        '<div id="recommendations"><img data-old-hires="https://m.media-amazon.com/images/I/81WRONG1.jpg" '
        'data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/81WRONG2.jpg&quot;:[1000,1500]}"></div>'
        + main
    )
    assert _image_from_detail(markup) == expected


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (
            [
                {"variant": "BACK", "hiRes": "https://m.media-amazon.com/images/I/81BACK01.jpg"},
                {"variant": "MAIN", "hiRes": "https://m.media-amazon.com/images/I/81MAIN01.jpg"},
            ],
            "81MAIN01",
        ),
        (
            [
                {"hiRes": "https://m.media-amazon.com/images/I/81OTHER1.jpg"},
                {"variant": "MAIN", "hiRes": "https://m.media-amazon.com/images/I/81MAIN01.jpg"},
            ],
            "81MAIN01",
        ),
        ([{"hiRes": "https://m.media-amazon.com/images/I/81MAIN01.jpg"}], "81MAIN01"),
        (
            [
                {"variant": "BACK", "hiRes": "https://m.media-amazon.com/images/I/81BACK01.jpg"},
                {"hiRes": "https://m.media-amazon.com/images/I/81OTHER1.jpg"},
            ],
            None,
        ),
        ([{"variant": "BACK", "hiRes": "https://m.media-amazon.com/images/I/81BACK01.jpg"}], None),
        (
            [
                {
                    "variant": "MAIN",
                    "hiRes": None,
                    "large": "https://m.media-amazon.com/images/I/81MAIN01._SL500_.jpg",
                }
            ],
            "81MAIN01",
        ),
        (
            [
                {
                    "variant": "MAIN",
                    "hiRes": "https://m.media-amazon.com/images/I/01RmK%2BJ4pJL.gif",
                    "large": "https://m.media-amazon.com/images/I/81MAIN01.jpg",
                }
            ],
            "81MAIN01",
        ),
        ([], None),
    ],
)
def test_detail_gallery_selects_main_image(entries: list[dict[str, object]], expected: str | None) -> None:
    gallery = json.dumps(entries)
    markup = f"<script>var data = {{'colorImages': {{'initial': {gallery}}}}};</script>"
    assert _image_from_detail(markup) == expected


def test_detail_gallery_hires_precedes_main_thumbnail_src() -> None:
    markup = (
        '<img id="landingImage" src="https://m.media-amazon.com/images/I/31SMALL1.jpg">'
        '<script>var data = {"colorImages":{"initial":[{"variant":"MAIN",'
        '"hiRes":"https://m.media-amazon.com/images/I/81MAIN01.jpg"}]}};</script>'
    )
    assert _image_from_detail(markup) == "81MAIN01"


@pytest.mark.parametrize("with_main_gallery", [False, True])
def test_detail_gallery_ignores_recommendation_container(with_main_gallery: bool) -> None:
    markup = (
        '<div id="recommendations"><script>var data = {"colorImages":{"initial":[{"variant":"MAIN",'
        '"hiRes":"https://m.media-amazon.com/images/I/81WRONG1.jpg"}]}};</script></div>'
    )
    if with_main_gallery:
        markup += (
            '<script>var data = {"colorImages":{"initial":[{"variant":"MAIN",'
            '"hiRes":"https://m.media-amazon.com/images/I/81MAIN01.jpg"}]}};</script>'
        )
    assert _image_from_detail(markup) == ("81MAIN01" if with_main_gallery else None)


@pytest.mark.parametrize(
    "markup",
    [
        '<script>var other = {"hiRes":"https://m.media-amazon.com/images/I/81WRONG1.jpg"};</script>',
        '<script>var data = {"colorImages":{"initial":[{"variant":"MAIN","hiRes":'
        '"https://m.media-amazon.com/images/I/81WRONG1.jpg"}</script>',
        '<script>var data = {"colorImages":{"initial":[{"variant":"MAIN","hiRes":'
        '(() => "https://m.media-amazon.com/images/I/81WRONG1.jpg")()}]}};</script>',
        '<script>var data = {"colorImages":{"initial":"https://m.media-amazon.com/images/I/81WRONG1.jpg"}};</script>',
        '<script>var data = {"colorImages":{"other":[{"variant":"MAIN",'
        '"hiRes":"https://m.media-amazon.com/images/I/81WRONG1.jpg"}]}};</script>',
    ],
)
def test_detail_gallery_ignores_unrelated_or_non_json_script(markup: str) -> None:
    assert _image_from_detail(markup) is None


def test_detail_image_falls_back_from_malformed_dynamic_json() -> None:
    markup = (
        '<img id="landingImage" data-a-dynamic-image="{broken" src="https://m.media-amazon.com/images/I/81MAIN01.jpg">'
    )
    assert _image_from_detail(markup) == "81MAIN01"
