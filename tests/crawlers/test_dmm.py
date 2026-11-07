"""DMM 来源的图片候选地址与 cid → 番号.

分类目录改写、候选顺序与 cid 形态是 DMM 详情页与 r18 dump 共用的契约; 页面解析走 TOML 数据驱动.
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from parsel import Selector

from amane.crawlers.sites.dmm import DmmCrawler, _cover_images, _cover_urls, _extrafanart_urls
from amane.crawlers.sites.dmm_cdn import image_candidates, poster_candidates, thumb_candidates

if TYPE_CHECKING:
    from amane.crawlers.http import HttpClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("package", "expected_thumb", "expected_poster"),
    [
        (
            {
                "largeUrl": "https://pics.dmm.co.jp/digital/video/mide00845/mide00845pl.jpg?w=384",
                "mediumUrl": "https://pics.dmm.co.jp/digital/video/mide00845/mide00845ps.jpg?w=147",
            },
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/mide00845/mide00845pl.jpg",
                "https://pics.dmm.co.jp/digital/video/mide00845/mide00845pl.jpg",
                "https://pics.dmm.co.jp/digital/video/mide00845/mide00845pl.jpg?w=384",
            ],
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/mide00845/mide00845ps.jpg",
                "https://pics.dmm.co.jp/digital/video/mide00845/mide00845ps.jpg",
                "https://pics.dmm.co.jp/digital/video/mide00845/mide00845ps.jpg?w=147",
            ],
        ),
        (
            {"mediumUrl": "https://awsimgsrc.dmm.co.jp/pics_dig/digital/amateur/ako254/ako254jp.jpg?w=384"},
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/amateur/ako254/ako254jp.jpg",
                "https://pics.dmm.co.jp/digital/amateur/ako254/ako254jp.jpg",
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/amateur/ako254/ako254jp.jpg?w=384",
            ],
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/amateur/ako254/ako254jp.jpg",
                "https://pics.dmm.co.jp/digital/amateur/ako254/ako254jp.jpg",
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/amateur/ako254/ako254jp.jpg?w=384",
            ],
        ),
        (None, [], []),
    ],
)
async def test_digital_graphql_uses_full_size_image_candidates(
    package: dict[str, str] | None, expected_thumb: list[str], expected_poster: list[str]
) -> None:
    client = SimpleNamespace(
        post_json=AsyncMock(return_value={"data": {"ppvContent": {"title": "作品", "packageImage": package}}})
    )
    crawler = DmmCrawler(cast("HttpClient", client))
    result = await crawler._scrape_digital("https://video.dmm.co.jp/av/content/?id=mide00845", {})
    assert result is not None
    assert result.thumb_urls == expected_thumb
    assert result.poster_urls == expected_poster


@pytest.mark.parametrize(
    ("image_url", "expected"),
    [
        (
            "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg",
                "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg",
            ],
        ),
        (
            "https://pics.dmm.co.jp/mono/movie/118abf295r/118abf295rpl.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295r/118abf295rpl.jpg",
                "https://pics.dmm.co.jp/mono/movie/118abf295r/118abf295rpl.jpg",
            ],
        ),
        (
            "https://pics.dmm.co.jp/mono/movie/japanese/n_617db0242/n_617db0242pl.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/n_617db0242/n_617db0242pl.jpg",
                "https://pics.dmm.co.jp/mono/movie/japanese/n_617db0242/n_617db0242pl.jpg",
            ],
        ),
        (
            "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497pl.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg",
                "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497pl.jpg",
            ],
        ),
        (
            "https://pics.dmm.co.jp/digital/amateur/ako254/ako254jp.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/amateur/ako254/ako254jp.jpg",
                "https://pics.dmm.co.jp/digital/amateur/ako254/ako254jp.jpg",
            ],
        ),
        (
            "https://pics.dmm.co.jp/digital/e-book/b044appnt00019/b044appnt00019pl.jpg",
            ["https://pics.dmm.co.jp/digital/e-book/b044appnt00019/b044appnt00019pl.jpg"],
        ),
        ("https://pics.dmm.co.jp/p/ms/top/pic_packing.jpg", ["https://pics.dmm.co.jp/p/ms/top/pic_packing.jpg"]),
        # 已在高清 CDN 上的地址不重复推导.
        (
            "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg",
            ["https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg"],
        ),
        # 原图与可逆的标准副本在前, 来源缩放地址保留为最后兜底.
        (
            "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg?w=384&h=288&t=margin&f=webp",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg",
                "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497pl.jpg",
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg?w=384&h=288&t=margin&f=webp",
            ],
        ),
        (
            "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg?w=384&h=288",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg",
                "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg",
                "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg?w=384&h=288",
            ],
        ),
        ("https://javdb.com/cover.jpg", ["https://javdb.com/cover.jpg"]),
        # 段数不足的异常路径仍构造高清候选, 由下载期按序回退.
        (
            "https://pics.dmm.co.jp/mono/movie/broken.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/broken.jpg",
                "https://pics.dmm.co.jp/mono/movie/broken.jpg",
            ],
        ),
    ],
)
def test_image_candidates(image_url: str, expected: list[str]) -> None:
    assert image_candidates(image_url) == expected


@pytest.mark.parametrize(
    ("cover_url", "expected"),
    [
        (
            "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295ps.jpg",
                "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295ps.jpg",
            ],
        ),
        # 已在高清 CDN 上的封面同样只换成 ps 形态.
        (
            "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497ps.jpg",
                "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497ps.jpg",
            ],
        ),
        (
            "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg?w=384&h=288&t=margin&f=webp",
            ["https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295ps.jpg"],
        ),
        # 非 pl 形态 (含 ps 本身) 不给海报候选.
        ("https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295ps.jpg", []),
        ("https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497jp-1.jpg", []),
        ("", []),
    ],
)
def test_poster_candidates(cover_url: str, expected: list[str]) -> None:
    assert poster_candidates(cover_url) == expected


@pytest.mark.parametrize(
    ("image_url", "expected"),
    [
        (
            "//pics.dmm.co.jp/digital/video/ssis00497/ssis00497ps.jpg",
            [
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg",
                "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497pl.jpg",
                "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497ps.jpg",
                "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497ps.jpg",
            ],
        ),
        ("https://other.example/coverps.jpg?w=147", ["https://other.example/coverps.jpg?w=147"]),
        ("https://pics.dmm.co.jp.evil.example/coverps.jpg", ["https://pics.dmm.co.jp.evil.example/coverps.jpg"]),
        ("", []),
    ],
)
def test_thumb_candidates_only_promote_dmm_package_images(image_url: str, expected: list[str]) -> None:
    assert thumb_candidates(image_url) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["digital", "fanza_tv"])
@pytest.mark.parametrize("available", ["small", "large"])
async def test_api_missing_package_variant_uses_known_counterpart(source: str, available: str) -> None:
    large = "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497pl.jpg"
    small = "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497ps.jpg"
    if source == "digital":
        package = {"mediumUrl": small} if available == "small" else {"largeUrl": large}
        response = {"data": {"ppvContent": {"title": "作品", "packageImage": package}}}
        url = "https://video.dmm.co.jp/av/content/?id=ssis00497"
    else:
        package = {"packageImage": small} if available == "small" else {"packageLargeImage": large}
        response = {"data": {"fanzaTvPlus": {"content": {"title": "作品", **package}}}}
        url = "https://tv.dmm.co.jp/vod/detail/?content=ssis00497"
    client = SimpleNamespace(post_json=AsyncMock(return_value=response))
    crawler = DmmCrawler(cast("HttpClient", client))
    meta = await crawler._scrape(url)
    assert meta is not None
    assert meta.thumb_urls[:2] == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg",
        large,
    ]
    assert meta.poster_urls == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497ps.jpg",
        small,
    ]
    if available == "small":
        assert meta.thumb_urls[-1] == small


@pytest.mark.asyncio
async def test_dmm_tv_preserves_distinct_key_visual_and_package_images() -> None:
    package = "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg?w=384"
    portrait = "https://tv-images.example/key-visual.jpg?signature=retained"
    client = SimpleNamespace(
        post_json=AsyncMock(
            return_value={"data": {"video": {"titleName": "作品", "packageImage": package, "keyVisualImage": portrait}}}
        )
    )
    meta = await DmmCrawler(cast("HttpClient", client))._scrape("https://tv.dmm.com/vod/detail/?seasonId=123")
    assert meta is not None
    assert meta.thumb_urls == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg",
        "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497pl.jpg",
        package,
    ]
    assert meta.poster_urls == [portrait]


def test_html_cover_candidates_keep_main_gallery_and_metadata_sources() -> None:
    html = Selector(
        text=(
            '<script type="application/ld+json">{"@type":"Product","image":['
            '{"@type":"ImageObject","contentUrl":"https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295ps.jpg"},'
            "null,1]}</script>"
            '<meta property="og:image" content="https://pics.dmm.co.jp/digital/video/abf00295/abf00295pl.jpg">'
            '<div id="sample-image-block"><img data-lazy="https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/'
            '118abf295/118abf295pl.jpg?w=384">'
            '<img src="https://pics.dmm.co.jp/digital/video/abf00295/abf00295jp-1.jpg"></div>'
        )
    )
    covers = _cover_urls(html)
    assert covers == [
        "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295ps.jpg",
        "https://pics.dmm.co.jp/digital/video/abf00295/abf00295pl.jpg",
        "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg?w=384",
    ]
    thumbs, posters = _cover_images(covers)
    assert thumbs[0] == "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg"
    assert posters[0] == "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295ps.jpg"
    assert len(thumbs) == len(set(thumbs))
    assert len(posters) == len(set(posters))
    assert all("jp-1" not in url for url in [*thumbs, *posters])


@pytest.mark.parametrize(
    "markup",
    [
        '<script type="application/ld+json">[1,2,3]</script>',
        '<script type="application/ld+json">{"image":null}</script>',
        '<script type="application/ld+json">{"image":{"invalid":"value"}}</script>',
        '<script type="application/ld+json">broken</script>',
        '<script type="application/ld+json">{"@type":"Organization","image":"https://pics.dmm.co.jp/logo.jpg"}'
        "</script>",
        '<div id="sample-image-block"><a href="https://example.com/wrongpl.jpg">別の商品</a>'
        '<img src="https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497jp-1.jpg"></div>',
    ],
)
def test_html_cover_candidates_ignore_invalid_metadata_and_samples(markup: str) -> None:
    assert _cover_urls(Selector(text=markup)) == []


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # 通販旧块: a/@href 给中等尺寸, 归一到 jp-N.
        (
            '<div id="sample-image-block"><a href="https://pics.dmm.co.jp/digital/video/118abf295/118abf295-1.jpg">'
            "<img></a></div>",
            ["https://pics.dmm.co.jp/digital/video/118abf295/118abf295jp-1.jpg"],
        ),
        # レンタル块: img/@src 给中等尺寸.
        (
            '<div id="sample-image-block"><a name="sample-image" id="sample-image1">'
            '<img src="https://pics.dmm.co.jp/digital/video/118abf295/118abf295-2.jpg"></a></div>',
            ["https://pics.dmm.co.jp/digital/video/118abf295/118abf295jp-2.jpg"],
        ),
        # 月額块: img/@src 已给高清地址并带缩放参数; 首图是封面, 跳过.
        (
            '<div id="sample-image-block">'
            '<a class="js-image-modal"><img src="https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg'
            '?w=384&amp;h=288&amp;t=margin&amp;f=webp"></a>'
            '<a class="js-image-modal"><img src="https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497jp-1.jpg'
            '?w=384&amp;h=288&amp;t=margin&amp;f=webp"></a></div>',
            ["https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497jp-1.jpg"],
        ),
        # 高清地址给中等尺寸时同样归一到 jp-N.
        (
            '<div id="sample-image-block"><a><img src="https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/adn00431/adn00431-3.jpg"></a></div>',
            ["https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/adn00431/adn00431jp-3.jpg"],
        ),
        # 没有样品图块时不给剧照.
        ('<div id="sample-image-block"></div>', []),
        ("<html><body><p>no gallery</p></body></html>", []),
    ],
)
def test_extrafanart_urls(html: str, expected: list[str]) -> None:
    assert _extrafanart_urls(Selector(text=html)) == expected


@pytest.mark.parametrize(
    ("cid", "retail", "expected"),
    [
        # 通販 / レンタル cid 在番号前带厂商代码, 番号按原位数保留.
        ("118abf295", True, "ABF-295"),
        ("118abf295r", True, "ABF-295"),
        ("118abf007r", True, "ABF-007"),
        ("1dldss542", True, "DLDSS-542"),
        ("1sods098", True, "SODS-098"),
        ("9spone001", True, "SPONE-001"),
        ("125umd1029", True, "UMD-1029"),
        ("h_1059godr1287", True, "GODR-1287"),
        # FANZA 限定版标记与番号后缀不参与番号.
        ("d2gvh891", True, "GVH-891"),
        ("1fns264tk1", True, "FNS-264"),
        ("1sgki108v", True, "SGKI-108"),
        # 配信 cid 无厂商代码, 编号是 5 位补零.
        ("ssis00497", False, "SSIS-497"),
        ("sora00652a", False, "SORA-652"),
        ("adn00431", False, "ADN-431"),
        # 数字开头的番号只出现在配信柜台.
        ("13dsvr01968", False, "13DSVR-1968"),
        # 无法判定时返回 cid 大写.
        ("abc", True, "ABC"),
        ("118", True, "118"),
        ("", True, ""),
    ],
)
def test_cid_to_number(cid: str, retail: bool, expected: str) -> None:
    assert DmmCrawler._cid_to_number(cid, retail=retail) == expected
