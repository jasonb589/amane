"""Tenhow 高清海报源 (原生实现, 由 jasonb589.tenhow 插件迁入).

Tenhow 是静态站, 没有搜索接口:

- 每张封面是 ``images/<Amazon ASIN>.jpg`` (1055x1500 左右, 比多数站点的缩略图大得多);
- 每个作品块带 DMM 链接, 通販是 ``cid=jur517``, 配信是 ``id=jur00517`` (联盟链接里是 URL 编码的);
- 演员页名是罗马音; 五十音目录与分类页提供演员名到页面的映射, 目录优先补全.

因此本源在工厂缓存的爬虫实例中维护「演员名 → 页面」索引, 按请求预算逐步补全并定期重建。
命中判定只看番号: 演员页里所有 ``cid`` / ``id`` 归一化后与 ``query.number`` 比对。

本源声明 ``NEEDS_PARTIAL``: 它在第二段执行, 拿聚合后的演员名与番号做检索, 只补充 ``poster_urls``
(不返回标题 / 剧情, 因此不会抢其它站点的字段)。
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from typing import TYPE_CHECKING, override
from urllib.parse import parse_qs, urljoin, urlsplit

from parsel import Selector

from ...enums import SiteName
from ...net.errors import FailureReason, RequestError
from ...plugins.models import SourceTrait
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery
from ..parsing import NumberMatch, number_match

if TYPE_CHECKING:
    from ...aggregate.models import AggregatedMetadata
    from ...config import SiteConfig
    from ..http import HttpClient

SITE = "https://tenhow.net"

# 索引有效期与规模: 到期后重建; 最多爬这么多页, 防止顺着链接扩散到全站.
_INDEX_TTL = 21 * 86400
_MAX_INDEX_PAGES = 1500
# 未命中时一次刮削顺带补的索引页数 (每页约 40~120KB).
_MAX_PAGES_PER_FETCH = 12
# 索引还小的时候一次补更多页: 冷启动若只攒 12 页, 要几十次刮削才轮得到分类页里的演员.
_COLD_START_PAGES = 60
_COLD_START_INDEX_SIZE = 300
# 演员页解析结果的内存缓存 (同一演员的多个条目不必重复抓).
_PAGE_CACHE_TTL = 6 * 3600
_MAX_CACHED_PAGES = 128

_ASIN_RE = re.compile(r"/images/(B0[A-Z0-9]{8})\.jpg")
_PAGE_RE = re.compile(r"/([a-z0-9_-]+)\.html")
_CID_RE = re.compile(r"(?:^|/)cid=([0-9A-Za-z_-]+)/?")
_ACTRESS_TITLE_RE = re.compile(r"^(.*?)(?:の)?アダルトDVDパケ写")
_DIRECTORY_PAGES = frozenset(
    {"mokuji", "agyo", "kagyo", "sagyo", "tagyo", "nagyo", "hagyo", "magyo", "yagyo", "ragyo", "wagyo"}
)


def _norm_name(name: str) -> str:
    """演员名归一化: 去掉半角/全角空白, 便于与页面锚文本比对."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or ""))


def _keys(raw: str) -> set[str]:
    """番号或 cid → 可比对的 key 集合.

    DMM 的 h_/n_ 柜台前缀不属于目录号; 开头数字段保留给精确匹配,
    仅差数字段时由候选集合判定唯一性, 避免两个素人系列互相匹配.
    """
    plain = unicodedata.normalize("NFKC", raw or "").lower().replace("-", "").replace(" ", "")
    plain = re.sub(r"^[hn]_\d+", "", plain)
    match = re.fullmatch(r"(\d*)([a-z]+)(\d+)", plain)
    if match is None:
        return set()
    prefix, alpha, digits = match.groups()
    return {plain, f"{prefix}{alpha}{digits.lstrip('0') or '0'}"}


def _local_path(url: str, base_url: str = SITE) -> str | None:
    """仅接受站内 HTTP 链接; 归一相对地址后校验主机."""
    try:
        parsed = urlsplit(urljoin(f"{base_url}/", url))
        host = urlsplit(base_url).hostname
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            return None
        if parsed.hostname not in {host, f"www.{host}"}:
            return None
    except ValueError:
        return None
    return parsed.path


def _page_name(url: str, base_url: str = SITE) -> str | None:
    path = _local_path(url, base_url)
    match = _PAGE_RE.fullmatch(path or "")
    return match.group(1) if match else None


def _cover_asin(url: str, base_url: str = SITE) -> str | None:
    path = _local_path(url, base_url)
    match = _ASIN_RE.fullmatch(path or "")
    return match.group(1) if match else None


def _link_cid(url: str) -> str | None:
    """解码联盟链接后仅提取 DMM 商品标识, 不接受广告里的同名参数."""
    for _ in range(3):
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            return None
        query = parse_qs(parsed.query)
        host = parsed.hostname or ""
        if host in {"al.fanza.co.jp", "al.dmm.co.jp", "al.dmm.com"}:
            targets = query.get("lurl", [])
            if len(targets) != 1:
                return None
            url = targets[0]
            continue
        if host != "dmm.co.jp" and not host.endswith(".dmm.co.jp"):
            return None
        match = _CID_RE.search(parsed.path)
        cid = match.group(1) if match else next(iter(query.get("id", query.get("cid", []))), "")
        return cid if _keys(cid) else None
    return None


def _cid_map(html: str, base_url: str = SITE) -> dict[str, str]:
    """按作品容器绑定 CID 与封面, 不跨越相邻作品的缺图边界."""
    document = Selector(text=html)
    out: dict[str, str] = {}
    conflicts: set[str] = set()
    for cover in document.css("a[href]"):
        asin = _cover_asin(cover.attrib["href"], base_url)
        if asin is None:
            continue
        block = cover.xpath('ancestor::*[contains(concat(" ", normalize-space(@class), " "), " um_image_a ")][1]')
        if not block:
            block = cover.xpath("..")
        if block.xpath("name()").get() in {"html", "body"}:
            continue
        links = block.css("a[href]::attr(href)").getall()
        images = {value for link in links if (value := _cover_asin(link, base_url))}
        if images != {asin}:
            continue
        for cid in {value for link in links if (value := _link_cid(link))}:
            if cid in out and out[cid] != asin:
                conflicts.add(cid)
            out.setdefault(cid, asin)
    for cid in conflicts:
        out.pop(cid, None)
    return out


def _names_on_page(html: str, page: str, base_url: str = SITE) -> dict[str, str]:
    """从一页收集「演员名 → 页面」; 演员页自身用页面标题补一条."""
    found: dict[str, str] = {}
    document = Selector(text=html)
    title = document.css("title::text").get()
    if title:
        name_match = _ACTRESS_TITLE_RE.match(title.strip())
        if name_match:
            found.setdefault(_norm_name(name_match.group(1)), page)
    for link in document.css("a[href]"):
        target = _page_name(link.attrib["href"], base_url)
        name = _norm_name(link.xpath("string(.)").get() or "")
        if target and name:
            found.setdefault(name, target)
    return found


def _select_asin(mapping: dict[str, str], wanted: set[str]) -> str | None:
    exact: set[str] = set()
    loose: set[str] = set()
    for cid, asin in mapping.items():
        keys = _keys(cid)
        if keys & wanted:
            exact.add(asin)
        elif any(number_match(key, query) is NumberMatch.WITHOUT_LEADING_DIGITS for key in keys for query in wanted):
            loose.add(asin)
    candidates = exact or loose
    return next(iter(candidates)) if len(candidates) == 1 else None


def _actor_names(partial: AggregatedMetadata) -> list[str]:
    """保留聚合演员名与前序来源原文, 使译名不阻断日文目录检索."""
    names = [actor.name for actor in partial.actors]
    for entry in partial.raw.values():
        actors = entry.get("actors")
        if not isinstance(actors, list):
            continue
        for actor in actors:
            value = actor.get("name") if isinstance(actor, dict) else actor
            if isinstance(value, str):
                names.append(value)
    return list(dict.fromkeys(name for value in names if (name := _norm_name(value))))


class TenhowCrawler(Crawler):
    """只用演员名 + 番号补一张高清封面, 不提供其它字段."""

    def __init__(self, client: HttpClient, config: SiteConfig | None = None) -> None:
        super().__init__(client, config)
        self._index: dict[str, str] = {}
        self._pending: list[str] = []
        self._crawled: set[str] = set()
        self._updated = 0.0
        self._pages: dict[str, tuple[float, dict[str, str]]] = {}
        self._index_lock = asyncio.Lock()

    @classmethod
    @override
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(
            name=SiteName.TENHOW,
            base_url=SITE,
            urls=[SITE],
            traits=frozenset({SourceTrait.NEEDS_PARTIAL}),
        )

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        # 检索整段写在 fetch 里 (要读 partial_result), 基类的两段式接口在这里不用.
        return None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        return None

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        del options
        partial = query.partial_result
        if partial is None:
            return None
        names = _actor_names(partial)
        wanted = _keys(query.number)
        if not names or not wanted:
            return None

        if not self._index_lock.locked():
            self._expire_index()
        checked: set[str] = set()
        budget = self._page_budget()
        while True:
            for name in names:
                page = self._index.get(name)
                if page is None or page in checked:
                    continue
                checked.add(page)
                asin = await self._lookup(page, wanted)
                if asin is not None:
                    return MediaMetadata(
                        number=query.number,
                        poster_urls=[f"{self.base_url}/images/{asin}.jpg"],
                        source_url=f"{self.base_url}/{page}.html",
                        external_id=asin,
                    )
            missing = set(names) - self._index.keys()
            if not missing or budget <= 0 or (not self._pending and self._crawled):
                return None
            used = await self._extend_index(budget, targets=missing)
            budget -= used
            if not used and not (missing & self._index.keys()):
                return None

    def _expire_index(self) -> None:
        if self._updated and time.monotonic() - self._updated > _INDEX_TTL:
            self._index.clear()
            self._crawled.clear()
            self._pending.clear()
            self._pages.clear()
            self._updated = 0.0

    def _page_budget(self) -> int:
        """一次补索引的页数: 索引还很小时多补几页, 之后回落到固定值以控制请求量."""
        if len(self._index) < _COLD_START_INDEX_SIZE:
            return _COLD_START_PAGES
        return _MAX_PAGES_PER_FETCH

    async def _lookup(self, page: str, wanted: set[str]) -> str | None:
        cached = self._pages.get(page)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _PAGE_CACHE_TTL:
            mapping = cached[1]
        else:
            try:
                html = await self.client.get_html(f"{self.base_url}/{page}.html", cookies=self.cookies)
            except RequestError as exc:
                if exc.reason is not FailureReason.NOT_FOUND:
                    raise
                html = ""
            mapping = self._cache_page(page, html)
        return _select_asin(mapping, wanted)

    def _cache_page(self, page: str, html: str) -> dict[str, str]:
        mapping = _cid_map(html, self.base_url)
        if page not in self._pages and len(self._pages) >= _MAX_CACHED_PAGES:
            del self._pages[min(self._pages, key=lambda key: self._pages[key][0])]
        self._pages[page] = (time.monotonic(), mapping)
        return mapping

    async def _extend_index(self, limit: int, *, targets: set[str] | None = None) -> int:
        async with self._index_lock:
            self._expire_index()
            if limit <= 0 or (targets is not None and not targets):
                return 0
            if not self._pending and not self._crawled:
                self._pending.append("index")
            done = 0
            while self._pending and done < limit and len(self._crawled) < _MAX_INDEX_PAGES:
                if targets and targets & self._index.keys():
                    break
                page = self._pending[0]
                if page in self._crawled:
                    self._pending.pop(0)
                    continue
                try:
                    html = await self.client.get_html(f"{self.base_url}/{page}.html", cookies=self.cookies)
                except RequestError as exc:
                    if exc.reason is not FailureReason.NOT_FOUND:
                        raise
                    html = ""
                self._pending.pop(0)
                self._crawled.add(page)
                if not self._updated:
                    self._updated = time.monotonic()
                self._cache_page(page, html)
                for name, target in _names_on_page(html, page, self.base_url).items():
                    self._index.setdefault(name, target)
                queued = set(self._pending)
                links = Selector(text=html).css("a[href]::attr(href)").getall()
                candidates = dict.fromkeys(
                    candidate for link in links if (candidate := _page_name(link, self.base_url))
                )
                for candidate in sorted(candidates, key=lambda value: value not in _DIRECTORY_PAGES):
                    if len(self._crawled) + len(queued) >= _MAX_INDEX_PAGES:
                        break
                    if candidate not in self._crawled and candidate not in queued:
                        self._pending.append(candidate)
                        queued.add(candidate)
                self._pending.sort(key=lambda value: value not in _DIRECTORY_PAGES)
                done += 1
            return done
