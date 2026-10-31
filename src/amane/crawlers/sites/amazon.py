"""Amazon.co.jp 高清海报源 (DVD 商品封面, 原生实现).

用**聚合后的日文标题**在 amazon.co.jp 的 DVD 类目检索, 取商品主图并换成大尺寸地址,
只补充 ``poster_urls`` —— 不返回标题 / 剧情, 因此不会抢其它站点的字段。

Amazon 的图片服务按地址后缀给尺寸: ``<ID>._SL1500_.jpg`` 长边 1500, 去掉后缀的
``<ID>.jpg`` 通常是原图 (DVD 封面常见 1000~1800 宽)。这里两个都给出, 下载端按顺序取
第一个成功的。

命中判据与别处一致: 结果标题必须**包含**聚合标题 (忽略空格与标点), 或相似度达标;
否则宁可返回 None, 也不给错封面。

注意: 这条源只对**日文标题**有意义 (有码 / 素人 / 里番的日文原名)。欧美条目在日亚上
是另一套邦题, 命中率很低 —— 那是 theporndb 的活。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import override
from urllib.parse import quote

from ...enums import SiteName
from ...plugins.models import SourceTrait
from ..base import Crawler, CrawlerProfile
from ..http import RequestError
from ..models import FetchOptions, MediaMetadata, SearchQuery

SITE = "https://www.amazon.co.jp"
_SEARCH_URL = SITE + "/s?k={term}&i=dvd"

# 结果块以 data-asin 分隔; 块内取第一张图与第一段 alt 文本 (alt 就是商品名).
_ASIN_SPLIT_RE = re.compile(r'data-asin="([A-Z0-9]{10})"')
_IMAGE_RE = re.compile(r"m\.media-amazon\.com/images/I/([A-Za-z0-9%+_-]{6,})")
_ALT_RE = re.compile(r'alt="([^"]{4,200})"')
_NOISE_RE = re.compile(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+")
_MIN_RATIO = 0.6
_MAX_CANDIDATES = 6


def _norm(text: str) -> str:
    """只留可读字符, 用于标题比对."""
    return _NOISE_RE.sub("", (text or "").lower())


def _title_matches(wanted: str, found: str) -> bool:
    """包含即算命中; 否则看相似度 (副标题差异不该判错)."""
    left, right = _norm(wanted), _norm(found)
    if len(left) < 4 or len(right) < 4:
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= _MIN_RATIO


def _parse_results(html: str) -> list[tuple[str, str, str]]:
    """搜索结果页 → [(asin, image_id, title)]; 按出现顺序, 最多 ``_MAX_CANDIDATES`` 条."""
    blocks = _ASIN_SPLIT_RE.split(html)
    results: list[tuple[str, str, str]] = []
    # split 后结构: [前缀, asin1, 块1, asin2, 块2, ...]
    for index in range(1, len(blocks) - 1, 2):
        asin, block = blocks[index], blocks[index + 1]
        image = _IMAGE_RE.search(block)
        title = _ALT_RE.search(block)
        if image is None or title is None:
            continue
        results.append((asin, image.group(1), title.group(1)))
        if len(results) >= _MAX_CANDIDATES:
            break
    return results


def _poster_urls(image_id: str) -> list[str]:
    """原图优先, 再退到长边 1500 的缩放版 (原图偶尔取不到)."""
    base = f"https://m.media-amazon.com/images/I/{image_id}"
    return [f"{base}.jpg", f"{base}._SL1500_.jpg"]


class AmazonCrawler(Crawler):
    """用日文标题补一张高清 DVD 封面, 不提供其它字段."""

    @classmethod
    @override
    def profile(cls) -> CrawlerProfile:
        # 不自定义 Accept-Language: 实测带 ``ja-JP`` 会被亚马逊判到 /age_check/ 年龄门,
        # 用 curl_cffi 指纹自带的请求头才能拿到真实搜索结果页.
        return CrawlerProfile(
            name=SiteName.AMAZON,
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
        title = (partial.title or "").strip() if partial is not None else ""
        if len(_norm(title)) < 4:
            return None

        try:
            html = await self.client.get_html(
                _SEARCH_URL.format(term=quote(title, safe="")), cookies=self.cookies or None
            )
        except RequestError as exc:
            self.logger.debug("amazon search failed", error=str(exc))
            return None

        results = _parse_results(html)
        best: tuple[float, str, str] | None = None
        for asin, image_id, found in results:
            if not _title_matches(title, found):
                continue
            score = SequenceMatcher(None, _norm(title), _norm(found)).ratio()
            if best is None or score > best[0]:
                best = (score, asin, image_id)
        self.logger.debug("amazon search", title=title, candidates=len(results))
        if best is None:
            return None

        _score, asin, image_id = best
        return MediaMetadata(
            number=query.number,
            poster_urls=_poster_urls(image_id),
            source_url=f"{SITE}/dp/{asin}",
            external_id=asin,
        )
