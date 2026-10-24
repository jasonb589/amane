"""Tenhow 高清海报源 (原生实现, 由 jasonb589.tenhow 插件迁入).

Tenhow 是静态站, 没有搜索接口:

- 每张封面是 ``images/<Amazon ASIN>.jpg`` (1055x1500 左右, 比多数站点的缩略图大得多);
- 每个作品块带 DMM 链接, 通販是 ``cid=jur517``, 配信是 ``id=jur00517`` (联盟链接里是 URL 编码的);
- 演员页名是罗马音 (``satsuki_mei.html``), 演员名 → 页面 没有索引页, 只能从各分类页的演员链接收集.

因此本源维护一份**进程内**的「演员名 → 页面」索引: 冷启动时从首页导航播种, 之后每次未命中
顺带补几页 (一次刮削最多 ``_MAX_PAGES_PER_FETCH`` 个请求), 索引随使用逐渐变全并定期重建。
命中判定只看番号: 演员页里所有 ``cid`` / ``id`` 归一化后与 ``query.number`` 比对。

本源声明 ``NEEDS_PARTIAL``: 它在第二段执行, 拿聚合后的演员名与番号做检索, 只补充 ``poster_urls``
(不返回标题 / 剧情, 因此不会抢其它站点的字段)。
"""

from __future__ import annotations

import re
import time
from typing import ClassVar, override

from ...enums import SiteName
from ...net.errors import RequestError
from ...plugins.models import SourceTrait
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery

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

_ASIN_RE = re.compile(r'href="images/(B0[A-Z0-9]{8})\.jpg"')
_LINK_RE = re.compile(r'href="([a-z0-9_]+)\.html"')
_PAIR_RE = re.compile(r'href="([a-z0-9_]+)\.html"[^>]*>([^<]{1,40})</a>')
_CID_RE = re.compile(r"cid(?:=|%3D)([0-9A-Za-z_-]+)")
_DIGITAL_RE = re.compile(r"[?&]id(?:=|%3D)([0-9A-Za-z_-]+)")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_ACTRESS_TITLE_RE = re.compile(r"^(.*?)(?:の)?アダルトDVDパケ写")


def _norm_name(name: str) -> str:
    """演员名归一化: 去掉半角/全角空白, 便于与页面锚文本比对."""
    return re.sub(r"[\s\u3000]+", "", name or "")


def _keys(raw: str) -> set[str]:
    """番号或 cid → 可比对的 key 集合.

    去掉符号并小写, 再补上「数字段去前导零」与「去掉厂商数字前缀」的形式:
    ``JUR-517`` / ``jur517`` / ``jur00517`` 因此都能互相比中.
    """
    plain = re.sub(r"[^0-9a-z]", "", (raw or "").lower())
    if not plain:
        return set()
    keys = {plain}
    match = re.fullmatch(r"(\d*)([a-z]+)(\d+)", plain)
    if match:
        prefix, alpha, digits = match.groups()
        variants = {digits, digits.lstrip("0") or "0"}
        keys.update(f"{prefix}{alpha}{item}" for item in variants)
        keys.update(f"{alpha}{item}" for item in variants)
    return keys


_UNESCAPE = {"3d": "=", "2f": "/", "26": "&", "3f": "?"}


def _unescape_link(match: re.Match[str]) -> str:
    return _UNESCAPE[match.group(1).lower()]


def _open_links(html: str) -> str:
    """还原联盟链接里的 URL 编码 (``%3D`` ``%2F`` ``%26`` ``%3F``), 便于直接按 ``cid=`` / ``id=`` 匹配."""
    return re.sub(r"%(3d|2f|26|3f)", _unescape_link, html, flags=re.IGNORECASE)


def _cid_map(html: str) -> dict[str, str]:
    """演员页 → {cid: ASIN}; 每个 cid 归属**最近的前一张**封面.

    插件版取的是「整页第一张」, 一个演员页有几十个作品时会全部指向同一张封面 (串图);
    这里改成最近一张, 与注释一致.
    """
    text = _open_links(html)
    images = [(match.start(), match.group(1)) for match in _ASIN_RE.finditer(text)]
    if not images:
        return {}
    out: dict[str, str] = {}
    for match in list(_CID_RE.finditer(text)) + list(_DIGITAL_RE.finditer(text)):
        nearest: str | None = None
        for position, asin in images:
            if position >= match.start():
                break
            nearest = asin
        if nearest is not None:
            out.setdefault(match.group(1), nearest)
    return out


def _names_on_page(html: str, page: str) -> dict[str, str]:
    """从一页收集「演员名 → 页面」; 演员页自身用页面标题补一条."""
    found: dict[str, str] = {}
    title_match = _TITLE_RE.search(html)
    if title_match:
        name_match = _ACTRESS_TITLE_RE.match(title_match.group(1).strip())
        if name_match:
            found.setdefault(_norm_name(name_match.group(1)), page)
    for match in _PAIR_RE.finditer(html):
        name = _norm_name(match.group(2))
        if name:
            found.setdefault(name, match.group(1))
    return found


class TenhowCrawler(Crawler):
    """只用演员名 + 番号补一张高清封面, 不提供其它字段."""

    # 进程内共享的索引状态 (插件版本写在 data_dir; 核心没有该概念, 进程存活期内够用).
    _index: ClassVar[dict[str, str]] = {}
    _pending: ClassVar[list[str]] = []
    _crawled: ClassVar[set[str]] = set()
    _updated: ClassVar[float] = 0.0
    _pages: ClassVar[dict[str, tuple[float, dict[str, str]]]] = {}

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
        if partial is None or not partial.actors:
            return None
        names = [name for name in (_norm_name(actor.name) for actor in partial.actors) if name]
        wanted = _keys(query.number)
        if not names or not wanted:
            return None

        for name in names:
            page = self._index.get(name)
            if page is None:
                continue
            asin = await self._lookup(page, wanted)
            if asin is not None:
                return MediaMetadata(
                    number=query.number,
                    poster_urls=[f"{SITE}/images/{asin}.jpg"],
                    source_url=f"{SITE}/{page}.html",
                    external_id=asin,
                )

        # 没命中: 顺带把索引补一点, 后面的条目命中率随之提高.
        if self._pending or not self._crawled:
            await self._extend_index(self._page_budget())
        return None

    def _page_budget(self) -> int:
        """一次补索引的页数: 索引还很小时多补几页, 之后回落到固定值以控制请求量."""
        if len(self._index) < _COLD_START_INDEX_SIZE:
            return _COLD_START_PAGES
        return _MAX_PAGES_PER_FETCH

    async def _lookup(self, page: str, wanted: set[str]) -> str | None:
        cached = self._pages.get(page)
        now = time.time()
        if cached is not None and now - cached[0] < _PAGE_CACHE_TTL:
            mapping = cached[1]
        else:
            try:
                html = await self.client.get_html(f"{SITE}/{page}.html", cookies=self.cookies)
            except RequestError:
                # 单页 404/5xx 只当没命中: 演员下架或页面改名, 不该让整个源判失败.
                self._pages[page] = (now, {})
                return None
            mapping = _cid_map(html)
            self._pages[page] = (now, mapping)
        for cid, asin in mapping.items():
            if _keys(cid) & wanted:
                return asin
        return None

    async def _extend_index(self, limit: int) -> None:
        if self._updated and time.time() - self._updated > _INDEX_TTL:
            self._index, self._crawled, self._pending = {}, set(), []
        if not self._pending and not self._crawled:
            seed = await self.client.get_html(f"{SITE}/index.html", cookies=self.cookies)
            self._crawled.add("index")
            for name, target in _names_on_page(seed, "index").items():
                self._index.setdefault(name, target)
            for match in _LINK_RE.finditer(seed):
                candidate = match.group(1)
                if candidate != "index.html" and candidate not in self._pending:
                    self._pending.append(candidate)
        done = 0
        while self._pending and done < limit:
            page = self._pending.pop(0)
            if page in self._crawled:
                continue
            try:
                html = await self.client.get_html(f"{SITE}/{page}", cookies=self.cookies)
            except RequestError:
                # 死链只跳过并计入已爬, 否则同一页会被反复排到队首, 索引再也长不大;
                # 也不能让补索引这条支线把整个源判成 404.
                self._crawled.add(page)
                done += 1
                continue
            self._crawled.add(page)
            for name, target in _names_on_page(html, page.removesuffix(".html")).items():
                self._index.setdefault(name, target)
            if len(self._crawled) < _MAX_INDEX_PAGES:
                for match in _LINK_RE.finditer(html):
                    candidate = match.group(1)
                    if candidate not in self._crawled and candidate not in self._pending:
                        self._pending.append(candidate)
            done += 1
        if done:
            self._updated = time.time()
