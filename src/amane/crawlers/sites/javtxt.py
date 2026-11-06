"""JAVTXT (javtxt.com): 按番号检索的**文本补充源**.

站点是服务端渲染的页面:
``/search?q=<番号>`` → 结果列表 (链接形如 ``/v/<id>``) → 详情页用
``<dd>标签</dd><dt>值</dt>`` 的 ``dl`` 结构摆放元信息.

本源的定位是「补字段」而不是「补图」:

- 提供: 日文原题 (交给管线按 field_language 翻译)、演员、发行日、系列、片商、厂牌、导演、
  类别 (中文标签)、**剧情日文原文** (``p.text-jp``; ``div.text-zh`` 是站点自带的机翻, 不用);
- 不提供: 封面 / 缩略图 / 预告片 / 时长 / 评分 —— 这些交给 dmm / amazon / mgstage 那些源.

注意站点对部分机房 IP 会直接断连, 拿不到时按网络错误上报, 由 browser 策略决定要不要升级到 solver.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from parsel import Selector

from ...enums import SiteName
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors
from ..parsing import fold_number

SITE = "https://javtxt.com"

#: 详情页链接: ``/v/396334`` (纯数字 id).
_DETAIL_HREF = re.compile(r'href="(?P<path>/v/\d+)"')

#: ``番号`` 那一格形如 ``ssis-313 (ssis00313)``; 只取前半段.
_NUMBER_RE = re.compile(r"([A-Za-z0-9]+-\d+)")

_LABEL_NUMBER = "番号"
_LABEL_RELEASE = "发行时间"
_LABEL_SERIES = "系列"
_LABEL_STUDIO = "片商"
_LABEL_LABEL = "厂牌"
_LABEL_PRODUCER = "导演"
_LABEL_GENRE = "类别"


def _text(node: Selector) -> str:
    return " ".join((node.xpath("string(.)").get() or "").split())


def _value_of(sel: Selector, label: str) -> str | None:
    """按 ``<dd>标签</dd><dt>值</dt>`` 取一格的值; 标签带 emoji 前缀, 用包含匹配."""
    for dd in sel.css("dl > dd"):
        if label not in _text(dd):
            continue
        value = _text(dd.xpath("following-sibling::dt[1]"))
        return value or None
    return None


def _genres(sel: Selector) -> list[str]:
    for dd in sel.css("dl > dd"):
        if _LABEL_GENRE not in _text(dd):
            continue
        tags = [_text(node) for node in dd.xpath("following-sibling::dt[1]//a")]
        return [tag for tag in tags if tag]
    return []


def parse_detail(html: str, url: str) -> MediaMetadata | None:
    """详情页 → MediaMetadata; 拿不到番号或标题时返回 None (宁可没有, 也不写空壳)."""
    sel = Selector(text=html or "")
    raw_number = _value_of(sel, _LABEL_NUMBER) or ""
    match = _NUMBER_RE.search(raw_number)
    if match is None:
        return None
    title = _text(sel.css("h1.text-jp")) or _text(sel.css("h1"))
    if not title:
        return None

    # 剧情: 只要日文原文. 站点自带的中文是机翻, 交给管线按提示词重译更好.
    plot = _text(sel.css("p.text-jp")) or None
    actors = [_text(node) for node in sel.css("dl.dl-actress a.actress")]
    return MediaMetadata(
        number=match.group(1).upper(),
        title=title,
        actors=film_actors([actor for actor in actors if actor]),
        studio=_value_of(sel, _LABEL_STUDIO),
        publisher=_value_of(sel, _LABEL_LABEL),
        release=_value_of(sel, _LABEL_RELEASE),
        tags=_genres(sel),
        series=_value_of(sel, _LABEL_SERIES),
        plot=plot,
        directors=[_value_of(sel, _LABEL_PRODUCER)] if _value_of(sel, _LABEL_PRODUCER) else [],
        source_url=url,
        external_id=url.rstrip("/").rsplit("/", 1)[-1],
    )


def detail_links(html: str) -> list[str]:
    """搜索结果页里的详情页链接 (绝对 URL, 按出现顺序去重)."""
    seen: list[str] = []
    for match in _DETAIL_HREF.finditer(html or ""):
        path = match.group("path")
        if path not in seen:
            seen.append(path)
    return [f"{SITE}{path}" for path in seen]


class JavtxtCrawler(Crawler):
    """按番号补中文/日文文本字段; 不提供任何图片."""

    #: 同一番号最多试几个搜索结果, 防止错条目把字段写脏.
    _MAX_CANDIDATES = 2

    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(
            name=SiteName.JAVTXT,
            base_url=SITE,
            urls=[SITE],
            # 与站内页面一致的导航头: 缺 Sec-Fetch-* 时部分节点会回空页.
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        # 检索整段写在 fetch 里 (要校验命中的番号), 基类的两段式接口在这里不用.
        return None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        del options
        return parse_detail(await self.client.get_html(url), url)

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        del options
        wanted = fold_number(query.number)
        html = await self.client.get_html(f"{SITE}/search?q={quote(query.number)}")
        for url in detail_links(html)[: self._MAX_CANDIDATES]:
            meta = parse_detail(await self.client.get_html(url), url)
            if meta is not None and fold_number(meta.number) == wanted:
                return meta
        return None
