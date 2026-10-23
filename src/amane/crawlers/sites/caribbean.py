"""カリビアンコム (Caribbeancom) 官方站: 无码主力源, 番号形如 ``122524-001`` / ``122524_01``.

站内两套模板并存, 字段都能拿到, **但图片策略不同**:

* 经典模板 (``-NNN``): 横向封面 ``/moviepages/{number}/images/l_l.jpg``, 剧照是真实
  ``<a href="…/images/l/NNN.jpg">`` 锚点 (数量以页面为准, 常见 5 张左右).
* 另一套模板 (``_NN``): 素材由页面 JS 从别的主机加载, 上面那个封面路径 **404**,
  页面上也没有剧照锚点 —— 因此这类条目只回文字字段, 图片交给其它来源, 不去猜不存在的 URL.

**番号绝不能改数字位**: ``122524_01`` 与 ``122524-001`` 是两部不同的片 (前者"性なる夜に…",
后者"母性溢れる…"). 因此先按原样请求; 只有当原样页面不是详情页时, 才换分隔符**再试一次**
(数字位保持不变), 避免把另一部片的元数据刮进来.
"""

from __future__ import annotations

import re
from datetime import date

from parsel import Selector

from ...enums import SiteName
from ...parsing.file_info import ContentType
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors
from ..parsing import extract_all_texts, extract_text

BASE_URL = "https://www.caribbeancom.com"

#: 老站点只发 EUC-JP, 不显式指定就整页乱码.
_ENCODING = "euc-jp"

#: 六位日期 + 分隔符 + 序号 (两种模板: ``122524-001`` / ``122524_01``).
_NUMBER = re.compile(r"^(?P<date>\d{6})(?P<sep>[-_])(?P<serial>\d{1,4})$")

#: 详情页 URL 里的番号 (两套模板都认, 且原样保留).
_DETAIL_PATH = re.compile(r"/moviepages/(?P<number>[^/]+)/index\.html")

#: 经典模板的横向封面.
_COVER = re.compile(r"/moviepages/(?P<number>[^/\"]+)/images/l_l\.jpg")

#: 剧照: 只认页面里真实写出的锚点, 不按序号猜 (猜出来的多半 404).
_FANART_ANCHOR = re.compile(r'href="(?P<path>/moviepages/(?P<number>[^/\"]+)/images/l/(?P<seq>\d{2,4})\.jpg)"')

#: 剧照最多保留的张数.
_FANART_LIMIT = 12

#: ISO8601 时长, 站点写作 ``T00H59M06S``.
_DURATION = re.compile(r"^P?(?:T)?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$")

#: 无码站只服务这两类: 其它类型不浪费一次请求.
_CONTENT_TYPES = frozenset({ContentType.UNCENSORED, ContentType.AMATEUR})

#: 详情页的判定标记: 命中不到这两样就是站点给的提示页.
_DETAIL_MARKERS = ("itemprop=\"name\"", "caribbeancom")


def split_number(number: str) -> tuple[str, str, str] | None:
    """``122524-001`` / ``122524_01`` → (日期段, 分隔符, 序号); 形态不符返回 ``None``."""
    match = _NUMBER.match(number.strip())
    if match is None:
        return None
    return match["date"], match["sep"], match["serial"]


def number_variants(number: str) -> list[str]:
    """原写法在前, 换分隔符的写法在后; **数字位不变**(换数字位就是另一部片)."""
    parts = split_number(number)
    if parts is None:
        return [number.strip()] if number.strip() else []
    date_part, sep, serial = parts
    other = f"{date_part}{'_' if sep == '-' else '-'}{serial}"
    return list(dict.fromkeys([f"{date_part}{sep}{serial}", other]))


def release_from_number(number: str) -> str | None:
    """配信日不在页面上: 番号前六位是 ``MMDDYY`` (``122524`` → 2024-12-25), 按 20xx 解释."""
    parts = split_number(number)
    if parts is None:
        return None
    digits = parts[0]
    try:
        return date(2000 + int(digits[4:6]), int(digits[0:2]), int(digits[2:4])).isoformat()
    except ValueError:
        return None


def runtime_minutes(raw: str) -> int | None:
    """ISO8601 时长 → 分钟 (有秒就进一分钟; 解析不出返回 ``None``)."""
    match = _DURATION.match(raw.strip())
    if match is None:
        return None
    minutes = int(match["h"] or 0) * 60 + int(match["m"] or 0) + (1 if int(match["s"] or 0) else 0)
    return minutes or None


def is_detail_page(text: str) -> bool:
    """站点对不存在的番号也回 200 提示页, 用详情页标记区分."""
    return all(marker in text for marker in _DETAIL_MARKERS)


def parse_detail(text: str, number: str, url: str) -> MediaMetadata | None:
    """详情页 HTML → 元数据; 认不出标题视为不匹配."""
    html = Selector(text=text)
    title = extract_text(html, "//h1[@itemprop='name']/text()", "//title/text()")
    # <title> 带站点名后缀 (``… | 無修正アダルト動画 カリビアンコム``), 取前半段.
    title = title.split("|", 1)[0].strip()
    if not title:
        return None

    actors = extract_all_texts(html, "//a[@itemprop='actor']//span[@itemprop='name']/text()")
    tags = extract_all_texts(html, "//a[@class='spec-item']/text()")
    duration = extract_text(html, "//span[@itemprop='duration']/@content")
    plot = extract_text(html, "//p[@itemprop='description']/text()")

    # 封面只在页面真的引用过时才给: 另一套模板上这个路径是 404, 猜了就是无谓的失败请求.
    cover = ""
    cover_match = _COVER.search(text)
    if cover_match is not None and cover_match["number"] == number:
        cover = f"{BASE_URL}{cover_match.group(0)}"

    # 剧照同理: 只取页面里写出的锚点.
    fanart: list[str] = []
    for match in _FANART_ANCHOR.finditer(text):
        if match["number"] != number:
            continue
        shot = f"{BASE_URL}{match['path']}"
        if shot not in fanart:
            fanart.append(shot)
        if len(fanart) >= _FANART_LIMIT:
            break

    return MediaMetadata(
        number=number,
        title=title,
        plot=plot or None,
        actors=film_actors(actors),
        tags=tags,
        studio="カリビアンコム",
        release=release_from_number(number),
        runtime=runtime_minutes(duration) if duration else None,
        # 站点只有横版图: 封面进 thumb, 同时作为 poster 的兜底 (竖版由管线裁切).
        thumb_urls=[cover] if cover else [],
        poster_urls=[cover] if cover else [],
        extrafanart=fanart,
        source_url=url,
    )


class CaribbeanCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.CARIBBEAN, base_url=BASE_URL)

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        if query.content_type not in _CONTENT_TYPES:
            return None
        for number in number_variants(query.number):
            url = f"{self.base_url}/moviepages/{number}/index.html"
            text = await self.client.get_html(url, encoding=_ENCODING)
            if text and is_detail_page(text):
                return url
        return None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        match = _DETAIL_PATH.search(url)
        if match is None:
            return None
        text = await self.client.get_html(url, encoding=_ENCODING)
        if not text:
            return None
        return parse_detail(text, match["number"], url)
