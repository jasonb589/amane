"""xChina 源: 详情页解析 (JSON-LD) 与域名归属.

这个来源没有番号检索能力 —— 只用「指定链接刮削」: ``serves_url`` 认自己的域名,
``_scrape`` 解析详情页.
"""

from __future__ import annotations

from amane.crawlers.sites.xchina import XchinaCrawler, parse_video

VIDEO_URL = "https://xchina.co/video/6ae6d6e5352c0"

VIDEO_OBJECT = (
    '{"@context":"https://schema.org","@type":"VideoObject","identifier":"6ae6d6e5352c0",'
    '"name":"人气巨乳御姐的三洞大乱斗",'
    '"contentRating":"R18","isFamilyFriendly":false,'
    '"thumbnailUrl":"https://r2.xchina.download/cover/6ae6d6e5352c0.webp",'
    '"uploadDate":"2026-11-01T14:32:10+08:00",'
    '"mainEntityOfPage":"https://xchina.co/video/6ae6d6e5352c0",'
    '"duration":"PT20M50S",'
    '"actor":[{"@type":"Person","name":"翁雨澄","url":"https://xchina.co/model/624db14972ff7"}]}'
)
BREADCRUMB = (
    '{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":['
    '{"@type":"ListItem","position":1,"name":"首页","item":"https://xchina.co/"},'
    '{"@type":"ListItem","position":2,"name":"视频","item":"https://xchina.co/videos"},'
    '{"@type":"ListItem","position":3,"name":"国产","item":"https://xchina.co/video-category/cn"},'
    '{"@type":"ListItem","position":4,"name":"糖心Vlog","item":"https://xchina.co/video-series/20"},'
    '{"@type":"ListItem","position":5,"name":"人气巨乳御姐的三洞大乱斗","item":"' + VIDEO_URL + '"}]}'
)


def _page(*blocks: str, title: str = "人气巨乳御姐的三洞大乱斗 - 糖心Vlog - 小黄书 xChina") -> str:
    scripts = "".join(f'<script type="application/ld+json">{block}</script>' for block in blocks)
    return (
        f'<html><head><meta property="og:title" content="{title}">'
        '<meta property="og:image" content="https://r2.xchina.download/cover/6ae6d6e5352c0.webp">'
        f"</head><body>{scripts}</body></html>"
    )


def test_parse_video_reads_jsonld_fields() -> None:
    meta = parse_video(_page(VIDEO_OBJECT, BREADCRUMB), VIDEO_URL)

    assert meta is not None
    assert meta.title == "人气巨乳御姐的三洞大乱斗"
    assert [actor.name for actor in meta.actors] == ["翁雨澄"]
    assert meta.series == "糖心Vlog"
    assert meta.release == "2026-11-01"
    # PT20M50S → 20 分钟 (不足一分钟的秒数不计)
    assert meta.runtime == 20
    assert meta.poster_urls == ["https://r2.xchina.download/cover/6ae6d6e5352c0.webp"]
    assert meta.external_id == "6ae6d6e5352c0"
    assert meta.source_url == VIDEO_URL


def test_parse_video_strips_crumb_suffix_from_og_title() -> None:
    """og:title 是「标题 - 分类 - 站点」时, 面包屑里出现过的分类尾巴也要剥掉."""
    meta = parse_video(_page(BREADCRUMB), VIDEO_URL)

    assert meta is not None
    assert meta.title == "人气巨乳御姐的三洞大乱斗"


def test_parse_video_keeps_dash_that_is_not_a_crumb() -> None:
    """面包屑里没有的分段不动: 标题自带的 `` - `` 不能被吃掉."""
    page = _page(BREADCRUMB, title="第二部 - 完全版 - 小黄书 xChina")

    meta = parse_video(page, VIDEO_URL)

    assert meta is not None
    assert meta.title == "第二部 - 完全版"


def test_parse_video_falls_back_to_og_tags() -> None:
    """ld+json 缺失时退回 og:title / og:image, 标题去掉站点后缀."""
    meta = parse_video(_page(), VIDEO_URL)

    assert meta is not None
    assert meta.title == "人气巨乳御姐的三洞大乱斗 - 糖心Vlog"
    assert meta.poster_urls == ["https://r2.xchina.download/cover/6ae6d6e5352c0.webp"]
    assert meta.runtime is None


def test_parse_video_rejects_page_without_title() -> None:
    assert parse_video("<html><body>blocked</body></html>", VIDEO_URL) is None


def test_parse_video_ignores_broken_jsonld() -> None:
    meta = parse_video(_page("{not json}", VIDEO_OBJECT), VIDEO_URL)
    assert meta is not None
    assert meta.title == "人气巨乳御姐的三洞大乱斗"


def test_serves_url_matches_site_and_mirror() -> None:
    crawler = XchinaCrawler(None)  # type: ignore[arg-type]

    assert crawler.serves_url(VIDEO_URL)
    assert crawler.serves_url("https://www.xchina.co/video/abc")
    assert crawler.serves_url("https://xchina.tv/video/abc")
    assert not crawler.serves_url("https://r2.xchina.download/cover/abc.webp")
    assert not crawler.serves_url("https://www.dmm.co.jp/mono/anime/-/detail/=/cid=68jvdw41/")
