"""xChina (小黄书) 视频页解析: 中文标题 / 演员 / 系列 / 封面 / 时长 / 发布日.

xChina 的条目**没有番号** (站内 ID 形如 ``6ae6d6e5352c0``), 所以本来源只提供两件事:
``serves_url`` (认得自己的域名) 与 ``_scrape`` (解析详情页). 用法就是「指定链接刮削」:
把 ``https://xchina.co/video/<id>`` 填进任务, 由这里解析. 普通按番号检索返回 None ——
站点按自己的 ID 组织, 用番号搜不到.

页面把结构化数据直接写在 ``application/ld+json`` 里 (schema.org VideoObject + BreadcrumbList),
比抓 DOM 稳得多:

- ``VideoObject``: ``name`` 标题 / ``thumbnailUrl`` 封面 / ``uploadDate`` 发布日 /
  ``duration`` ISO8601 时长 / ``actor[].name`` 演员 / ``identifier`` 站内 ID
- ``BreadcrumbList``: 分类 (``/video-category/...``) 与系列 (``/video-series/...``)

注意 xChina 在 Cloudflare 后面: 只有带完整浏览器头 (``Sec-Fetch-*`` / ``Accept-Language``)
的请求才会拿到 200, 光有 UA 会被判进 ``Just a moment...`` 挑战页. 因此 profile 里显式给出这些头.
"""

from __future__ import annotations

import json
import re
from typing import Any, override
from urllib.parse import urlparse

from ...enums import ActorGender, SiteName
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors

SITE = "https://xchina.co"
#: 同一站点的镜像域名 (跳转后仍是同一套页面结构).
MIRRORS = ("https://xchina.tv",)

_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
_OG_TITLE_RE = re.compile(r'property="og:title" content="([^"]+)"')
_OG_IMAGE_RE = re.compile(r'property="og:image" content="([^"]+)"')
_TITLE_SUFFIX = " - 小黄书 xChina"
_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

#: Cloudflare 只放行「像真导航」的请求: 少了这些头会被判进挑战页.
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def _jsonld_blocks(html: str) -> list[dict[str, Any]]:
    """页面里的 ld+json 块 → dict 列表; 解析失败的单块直接跳过."""
    blocks: list[dict[str, Any]] = []
    for match in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(match[1])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            blocks.append(data)
    return blocks


def _video_object(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for block in blocks:
        if block.get("@type") == "VideoObject":
            return block
    return None


def _crumb(blocks: list[dict[str, Any]], kind: str) -> str | None:
    """面包屑里指向 ``/video-<kind>/...`` 的那一级名字 (分类 / 系列)."""
    for block in blocks:
        if block.get("@type") != "BreadcrumbList":
            continue
        for item in block.get("itemListElement") or []:
            if not isinstance(item, dict):
                continue
            link = str(item.get("item") or "")
            if f"/video-{kind}/" in link:
                name = str(item.get("name") or "").strip()
                if name:
                    return name
    return None


def _runtime_minutes(duration: object) -> int | None:
    """ISO8601 时长 (``PT20M50S``) → 分钟; 不足一分钟按一分钟算."""
    if not isinstance(duration, str):
        return None
    match = _DURATION_RE.match(duration.strip().upper())
    if match is None:
        return None
    hours, minutes, seconds = (int(part) if part else 0 for part in match.groups())
    total = hours * 60 + minutes
    if seconds and total == 0:
        return 1
    return total or None


def _release_date(video: dict[str, Any]) -> str | None:
    """``uploadDate`` 的日期部分 (2026-11-01T14:32:10+08:00 → 2026-11-01)."""
    raw = video.get("uploadDate")
    if not isinstance(raw, str):
        return None
    head = raw.strip()[:10]
    return head if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head) else None


def parse_video(html: str, url: str) -> MediaMetadata | None:
    """详情页 → MediaMetadata; 解析不出标题时返回 None (宁可没有, 也不写空壳)."""
    blocks = _jsonld_blocks(html)
    video = _video_object(blocks)
    title = ""
    poster: list[str] = []
    actors: list[str] = []
    runtime: int | None = None
    release: str | None = None
    external_id: str | None = None
    source_url = url
    series: str | None = None
    if video is not None:
        title = str(video.get("name") or "").strip()
        thumb = video.get("thumbnailUrl")
        if isinstance(thumb, str) and thumb.strip():
            poster.append(thumb.strip())
        actors = [
            str(item.get("name") or "").strip()
            for item in (video.get("actor") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        runtime = _runtime_minutes(video.get("duration"))
        release = _release_date(video)
        raw_id = video.get("identifier")
        external_id = str(raw_id) if isinstance(raw_id, str) and raw_id.strip() else None
        page = video.get("mainEntityOfPage")
        if isinstance(page, str) and page.strip():
            source_url = page.strip()
        series = _crumb(blocks, "series")
    if not title:
        # 兜底: og:title 去掉站点后缀 (形如「标题 - 系列 - 小黄书 xChina」).
        match = _OG_TITLE_RE.search(html or "")
        if match is not None:
            title = match[1].replace(_TITLE_SUFFIX, "").strip()
        image = _OG_IMAGE_RE.search(html or "")
        if not poster and image is not None:
            poster.append(image[1].strip())
    title = title.strip()
    if not title:
        return None

    return MediaMetadata(
        number="",
        title=title,
        actors=film_actors(actors, gender=ActorGender.FEMALE),
        series=series,
        release=release,
        runtime=runtime,
        poster_urls=poster,
        external_id=external_id,
        source_url=source_url,
    )


class XchinaCrawler(Crawler):
    """xChina 详情页解析 (中文标题 / 演员 / 封面); 不参与番号检索."""

    @classmethod
    @override
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(
            name=SiteName.XCHINA,
            base_url=SITE,
            urls=[SITE, *MIRRORS],
            headers=dict(_BROWSER_HEADERS),
        )

    def serves_url(self, url: str) -> bool:
        """xchina.co / xchina.tv 及其子域, 外加图片 CDN (r2.xchina.download) 不算站点页."""
        host = urlparse(url).netloc.lower()
        if not host or host.endswith("xchina.download"):
            return False
        return host == "xchina.co" or host == "xchina.tv" or host.endswith((".xchina.co", ".xchina.tv"))

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        # 站点按自己的 ID 组织, 番号检索没有意义; 只支持「指定链接刮削」.
        return None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        del options
        html = await self.client.get_text(url)
        meta = parse_video(html, url)
        if meta is not None:
            self.logger.debug("xchina scraped", url=url, title=meta.title)
        return meta
