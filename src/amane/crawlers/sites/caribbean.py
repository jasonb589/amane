"""カリビアンコム (Caribbeancom) 官方站: 无码主力源, 番号形如 ``122524-001``.

页面是 EUC-JP 的老结构, 但字段都有 itemprop 标记, 抓取稳定:

* ``h1[itemprop=name]`` 标题 (已不含出演者名), ``p[itemprop=description]`` 剧情
* ``span[itemprop=duration]`` ISO8601 时长, ``a[itemprop=actor] span[itemprop=name]`` 出演者
* ``a.spec-item`` 标签 (ジャンル/オリジナル動画 等)
* 横版封面 ``/moviepages/{number}/images/l_l.jpg``, 剧照 ``/moviepages/{number}/images/l/NNN.jpg``

站点不给竖版海报, 也不给配信日: 海报由管线从封面按配置裁切, 配信日由番号前六位 (MMDDYY) 推出.

番号两种写法并存: javdb 用 ``122524_01``, 本家与 javbus 用 ``122524-001``; 这里统一成后者再去请求.
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

#: 六位日期 + 序号 (1~4 位, 分隔符可有可无).
_NUMBER = re.compile(r"^(?P<date>\d{6})[-_ ]?(?P<serial>\d{1,4})$")

#: 详情页 URL 里的番号 (已归一化形态).
_DETAIL_PATH = re.compile(r"/moviepages/(?P<number>\d{6}-\d{3})/")

#: 横版大图: 封面 (也是唯一的官方主图, 竖版海报靠管线裁切).
_COVER_PATH = "/moviepages/{number}/images/l_l.jpg"

#: 剧照 (sample gallery), 形如 ``/moviepages/122524-001/images/l/003.jpg``.
_FANART = re.compile(r"/moviepages/(?P<number>\d{6}-\d{3})/images/l/(?P<seq>\d{3,4})\.jpg")

#: 剧照最多保留的张数: 页面会给上百条, 全存下来没意义.
_FANART_LIMIT = 12

#: ISO8601 时长, 站点写作 ``T00H59M06S``.
_DURATION = re.compile(r"^P?(?:T)?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$")

#: 无码站只服务这两类: 其它类型不浪费一次请求.
_CONTENT_TYPES = frozenset({ContentType.UNCENSORED, ContentType.AMATEUR})


def canonical_number(number: str) -> str | None:
    """``122524_01`` / ``122524-1`` / ``122524001`` → ``122524-001``; 非本站形态返回 ``None``."""
    match = _NUMBER.match(number.strip())
    if match is None:
        return None
    return f"{match['date']}-{match['serial'].zfill(3)}"


def release_from_number(number: str) -> str | None:
    """配信日不在页面上: 番号前六位是 ``MMDDYY`` (``122524`` → 2024-12-25), 按 20xx 解释."""
    digits = number.split("-", 1)[0]
    if len(digits) != 6 or not digits.isdigit():
        return None
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

    cover = f"{BASE_URL}{_COVER_PATH.format(number=number)}"
    fanart: list[str] = []
    for match in _FANART.finditer(text):
        if match["number"] != number:
            continue
        shot = f"{BASE_URL}{match.group(0)}"
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
        thumb_urls=[cover],
        poster_urls=[cover],
        extrafanart=fanart,
        source_url=url,
    )


class CaribbeanCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.CARIBBEAN, base_url=BASE_URL)

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        number = canonical_number(query.number)
        if number is None or query.content_type not in _CONTENT_TYPES:
            return None
        url = f"{self.base_url}/moviepages/{number}/index.html"
        text = await self.client.get_html(url, encoding=_ENCODING)
        # 站点对不存在的番号也给 200 + 提示页, 因此用标题标记判定是否真的命中.
        if text and "itemprop=\"name\"" in text and "caribbeancom" in text:
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
