"""Amazon.co.jp DVD 海报源, 只补充图片与来源标识.

检索优先使用前序来源的日文原题; 聚合标题可能已经被翻译. 标题检索允许截短,
匹配仍依据完整标题, 并为有效番号保留回退预算. 仅凭番号命中时必须确认碟片标记.
图片限于匹配商品的主图, 原图与同 ASIN 的回退地址交由下载管线按实际像素选择.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, override
from urllib.parse import quote, unquote, urlsplit

from parsel import Selector

from ...enums import SiteName
from ...net.errors import FailureReason, SourceError
from ...plugins.models import SourceTrait
from ..base import Crawler, CrawlerProfile
from ..http import RequestError
from ..models import FetchOptions, MediaMetadata, SearchQuery

if TYPE_CHECKING:
    from ...aggregate.models import AggregatedMetadata

SITE = "https://www.amazon.co.jp"
_SEARCH_URL = SITE + "/s?k={term}&i=dvd"
#: 移动版搜索页: 成人条目**不给黑幕占位图**, 直接就是真封面 (实测 18 条全带 I/<id>),
#: 所以先走它; 桌面版只在移动版没命中时兜底.
_MOBILE_SEARCH_URL = SITE + "/gp/aw/s?k={term}&i=dvd"

_ASIN_RE = re.compile(r"[A-Z0-9]{10}")
_IMAGE_PATH_RE = re.compile(
    r"/images/I/([A-Za-z0-9+_-]{6,})(?:\._[A-Za-z0-9,+_-]+_)?\.(?:jpg|jpeg|png|webp|gif)", re.IGNORECASE
)
_IMAGE_HOSTS = frozenset(
    {
        "m.media-amazon.com",
        "images-na.ssl-images-amazon.com",
        "images-fe.ssl-images-amazon.com",
        "images-eu.ssl-images-amazon.com",
        "ecx.images-amazon.com",
    }
)
_GALLERY_RE = re.compile(r"""["']colorImages["']\s*:\s*\{\s*["']initial["']\s*:\s*""")
_NOISE_RE = re.compile(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+")
_MIN_RATIO = 0.6
#: 亚马逊的占位图 ID (60x40 的 loading gif / 1x1 透明图). 成人区卡片在年龄门确认前只有它们,
#: 取下来就是一张废图 —— 见到就当这个候选没有封面.
_PLACEHOLDER_IDS = frozenset({"01RmK+J4pJL", "01mI9NDJJTL"})
_MAX_KEYWORDS = 3
"""标题与番号共用的检索词预算."""

#: DMM / FANZA 商品标题里的促销与渠道噪声: 原样搜亚马逊会把正经条目挤出结果页.
_NOISE_TOKENS = (
    "視聴期限なし",
    "期間限定",
    "オンラインコード版",
    "オンラインコード",
    "ダウンロード版",
    "レンタル落ち",
    "特典生写真",
    "特典映像",
    "特典",
    "生写真",
    "送料無料",
    "アウトレット",
    "特別価格",
    "セール",
    "中古品",
    "中古",
)
_BRACKET_RE = re.compile(r"【[^】]*】|\[[^\]]*\]|〔[^〕]*〕|（[^）]*）|\([^)]*\)|《[^》]*》|〈[^〉]*〉")
_FORMAT_RE = re.compile(r"DVD|Blu-?ray|ブルーレイ|BD|4K|HD", re.IGNORECASE)
_PROMO_NOTE_RE = re.compile(r"視聴期限|期間限定|限定特典|特典.*(?:付|あり)|送料無料|オンラインコード|ダウンロード版")
_SEPARATOR_RE = re.compile(r"[|｜/／]+")
_CJK_RE = re.compile("[\u3040-\u30ff\u4e00-\u9fff]")
_NUMBER_RE = re.compile(r"(?<![a-z0-9_])([0-9]{0,3}[a-z]{2,12})[- ]?([0-9]{2,7})(?![a-z0-9_])", re.IGNORECASE)
_VOLUME_RE = re.compile(r"([前後上下])\s*[編篇巻]|(?:第\s*)?([0-9]+)\s*([巻話章])")
#: 真正的年龄门: 跳转 ``/age_check/`` 或带 ``driver-verify`` 的表单. 成人分区结果页里那个
#: ``Age Verification`` 只是 18 禁弹窗 (``black-curtain-popover``) 的标题, 不能当拦截判据.
_GATE_RE = re.compile(r'driver-verify|/age_check/|name="agecheck"', re.IGNORECASE)
#: 黑幕弹窗 ("page contains adult material ... Are you over 18?"): 未点「Yes」前成人卡片只给占位图.
_CURTAIN_RE = re.compile(r"black-curtain-popover|s-black-curtain", re.IGNORECASE)
#: 点「Yes」走的保存接口; ``returnUrl`` 是点了之后要回跳的页面.
_ELIGIBILITY_URL = SITE + "/-/en/black-curtain/save-eligibility/black-curtain?returnUrl={}"

#: 详情页的几个入口: 同一 ASIN 这几条 URL 的响应不总是等价, 空壳时换下一条再来.
_DETAIL_PATHS = ("/dp/{asin}", "/dp/{asin}?th=1&psc=1", "/-/en/dp/{asin}")
#: 只靠番号命中时要的碟片标记 / 要排除的周边条目 (同一番号的生写真、ポストカード也占结果位).
_DISC_RE = re.compile(r"DVD|Blu-?ray|ブルーレイ", re.IGNORECASE)
_GOODS_RE = re.compile("特典|生写真|ポストカード|クリアファイル|タオル|抱き枕|非売品|カレンダー|フィギュア")
_NUMBER_ONLY_SCORE = 0.2
"""番号单独命中的底分: 低于任何标题命中, 用来把「不带标题但确实是本片」的结果排在后面."""

#: 取日文原题的来源顺序: 日亚只能按原文检索, 拿译文 (中文/拼音) 搜必然 0 条.
_RAW_TITLE_ORDER = ("r18dev:jp", "dmm", "avbase", "javbus", "official")

_MAX_SEARCH_ATTEMPTS = 3
"""单个检索词的尝试次数: 亚马逊的 /age_check/ 年龄门是概率性的, 换一次请求常常就正常."""

_UNLOCK_LIMIT = 2
"""一次刮削里最多点几次黑幕弹窗的「Yes」: 每次多一个请求, 两次还没真封面就不再试."""


def _clean_bracket(match: re.Match[str]) -> str:
    text = match.group()[1:-1].strip()
    # 括号也承载作品名与卷次, 仅格式与促销备注可以整段排除.
    return " " if _FORMAT_RE.fullmatch(text) or _PROMO_NOTE_RE.search(text) else f" {text} "


def _clean(title: str) -> str:
    """清除促销与渠道备注, 保留括号内的作品名和卷次."""
    text = _BRACKET_RE.sub(_clean_bracket, unicodedata.normalize("NFKC", title or ""))
    for token in _NOISE_TOKENS:
        text = text.replace(token, " ")
    text = _SEPARATOR_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -‐—·・,，.。:：!")


def _keywords(title: str, number: str) -> list[str]:
    """标题由长至短, 为有效番号保留一个槽位; 纯拉丁标题不截短."""
    tokens = _clean(title).split()
    number = unicodedata.normalize("NFKC", number or "").strip()
    number_term = number if _NUMBER_RE.fullmatch(number) else ""
    title_budget = _MAX_KEYWORDS - bool(number_term)
    terms: list[str] = []
    seen: set[str] = set()
    for size in range(len(tokens), 0, -1):
        if size < len(tokens) and not _CJK_RE.search(" ".join(tokens[size:])):
            continue
        term = " ".join(tokens[:size])
        norm = _norm(term)
        if len(norm) < 4 or norm in seen:
            continue
        seen.add(norm)
        terms.append(term)
        if len(terms) >= title_budget:
            break
    if number_term and _norm(number_term) not in seen:
        terms.append(number_term)
    return terms


def _norm(text: str) -> str:
    """只留可读字符, 用于标题比对.

    先做 NFKC: 罗马数字 (Ⅷ → VIII)、全角字母/数字/半角假名都是兼容字符, 不折叠就会被当成噪声删掉,
    ``BEAUTY VENUS VIII`` 与亚马逊的 ``BEAUTY VENUS Ⅷ`` 会判成两个标题.
    """
    return _NOISE_RE.sub("", unicodedata.normalize("NFKC", text or "").lower())


def _volumes(title: str) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    for part, digits, unit in _VOLUME_RE.findall(unicodedata.normalize("NFKC", title)):
        key = ("前後" if part in {"前", "後"} else "上下") if part else unit
        values.setdefault(key, set()).add(part or str(int(digits)))
    return values


def _volume_conflict(wanted: str, found: str) -> bool:
    left, right = _volumes(wanted), _volumes(found)
    return any(left[key].isdisjoint(right[key]) for key in left.keys() & right.keys())


def _title_matches(wanted: str, found: str) -> bool:
    """包含即算命中; 否则比较相似度, 明确卷次冲突时拒绝."""
    left, right = _norm(_clean(wanted)), _norm(_clean(found))
    if len(left) < 4 or len(right) < 4 or _volume_conflict(wanted, found):
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= _MIN_RATIO


def _match_score(number: str, wanted: str, found: str) -> float:
    """候选结果的可取分数; ``0`` 表示不能用.

    标题命中按相似度计分; 只剩番号命中时给底分, 并且必须带碟片标记、不是特典周边 ——
    同一番号在亚马逊上还有一堆生写真 / ポストカード 条目, 取它们的图就是错封面.
    """
    if _volume_conflict(wanted, found):
        return 0.0
    number_match = _NUMBER_RE.fullmatch(unicodedata.normalize("NFKC", number).strip())
    number_hit = False
    if number_match is not None:
        prefix, digits = number_match.groups()
        numbers = _NUMBER_RE.findall(unicodedata.normalize("NFKC", found))
        same_prefix = [value for stem, value in numbers if stem.casefold() == prefix.casefold()]
        number_hit = digits in same_prefix
        if same_prefix and not number_hit:
            return 0.0
    if _GOODS_RE.search(found) and not _DISC_RE.search(found):
        return 0.0
    found_norm = _norm(_clean(found))
    titled = _title_matches(wanted, found)
    score = 0.0
    if number_hit:
        # 番号也在标题里: 里番/素人的番号位就是作品名, 这时按标题命中算, 不套碟片规则.
        if not titled and (not _DISC_RE.search(found) or _GOODS_RE.search(found)):
            return 0.0
        score = _NUMBER_ONLY_SCORE
    if titled:
        score += SequenceMatcher(None, _norm(_clean(wanted)), found_norm).ratio()
    return score


def _search_title(partial: AggregatedMetadata | None, number: str = "") -> str:
    """检索用的标题: 优先第一段来源里的**日文原题**, 再退到聚合标题, 最后退到番号位上的日文标题.

    ``field_language.title`` 设成中文时聚合标题是译名 (``义母 前篇``), 拿它搜日亚必然 0 条;
    日亚认的是原文 (``義母 前編`` / ``BEAUTY VENUS VIII``). 所以先看各来源的裸结果.
    里番没有番号, 文件名里的日文标题就落在 ``query.number`` 上 —— 全部来源都失手时也用它.
    """
    if partial is not None:
        raw = partial.raw
        for site in _RAW_TITLE_ORDER:
            entry = raw.get(site)
            if not isinstance(entry, dict):
                continue
            value = entry.get("title")
            if isinstance(value, str) and len(_norm(value)) >= 4:
                return value.strip()
        for site, entry in raw.items():
            if not site.endswith(":jp") or not isinstance(entry, dict):
                continue
            value = entry.get("title")
            if isinstance(value, str) and len(_norm(value)) >= 4:
                return value.strip()
        title = (partial.title or "").strip()
        if title:
            return title
    candidate = (number or "").strip()
    return candidate if _CJK_RE.search(candidate) else ""


def _parse_results(html: str) -> list[tuple[str, str, str]]:
    """解析整页商品卡片; 标题和图片必须属于同一 ASIN, 占位图交由详情页补充."""
    results: dict[str, tuple[str, str, str]] = {}
    for card in Selector(text=html).css("[data-asin]"):
        asin = card.attrib.get("data-asin", "")
        if not _ASIN_RE.fullmatch(asin):
            continue
        headings = [
            node for node in card.css("h2") if node.xpath("ancestor::*[@data-asin][1]/@data-asin").get() == asin
        ]
        title = next(
            (node.xpath("normalize-space(.)").get("") for node in headings if node.xpath("normalize-space(.)").get()),
            "",
        )
        images = [node for node in card.css("img") if node.xpath("ancestor::*[@data-asin][1]/@data-asin").get() == asin]
        product_images = [node for node in images if "s-image" in node.attrib.get("class", "").split()]
        if not product_images:
            # 无专用样式时, 仅采用标题相符的图片, 或卡片唯一的图片.
            product_images = [node for node in images if title and _title_matches(title, node.attrib.get("alt", ""))]
            if not product_images and len(images) == 1 and (not title or not images[0].attrib.get("alt", "").strip()):
                product_images = images
        if not title:
            title = next(
                (node.attrib.get("alt", "").strip() for node in product_images if node.attrib.get("alt", "").strip()),
                "",
            )
        if not title:
            continue
        image_id = next((image for node in product_images if (image := _image_on_node(node))), "")
        previous = results.get(asin)
        if previous is None or (not previous[1] and image_id):
            results[asin] = (asin, image_id, title)
    return list(results.values())


def _image_id(url: str) -> str | None:
    """从 Amazon 图片 CDN 地址提取 ID; 只解码一次, 保留 ID 内的加号."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.hostname not in _IMAGE_HOSTS
        or parsed.username
        or parsed.password
    ):
        return None
    match = _IMAGE_PATH_RE.fullmatch(unquote(parsed.path))
    if match is None or match[1] in _PLACEHOLDER_IDS:
        return None
    return match[1]


def _image_on_node(node: Selector, *, original_only: bool = False) -> str | None:
    if image := _image_id(node.attrib.get("data-old-hires", "")):
        return image
    try:
        dynamic: object = json.loads(node.attrib.get("data-a-dynamic-image", "{}"))
    except ValueError, RecursionError:
        dynamic = None
    best: tuple[int, str] | None = None
    if isinstance(dynamic, dict):
        for url, size in dynamic.items():
            if not isinstance(url, str) or not isinstance(size, list) or len(size) != 2:
                continue
            if not all(type(edge) is int and edge > 0 for edge in size):
                continue
            image = _image_id(url)
            if image and (best is None or size[0] * size[1] > best[0]):
                best = (size[0] * size[1], image)
    if best is not None:
        return best[1]
    if original_only:
        return None
    for attr in ("data-src", "data-lazy-src", "srcset", "data-srcset", "src"):
        for candidate in node.attrib.get(attr, "").split():
            if image := _image_id(candidate.rstrip(",")):
                return image
    return None


def _poster_urls(image_id: str) -> list[str]:
    """原图优先, 再退到长边 1500 的缩放版 (原图偶尔取不到)."""
    base = f"https://m.media-amazon.com/images/I/{image_id}"
    return [f"{base}.jpg", f"{base}._SL1500_.jpg"]


@dataclass
class _SearchState:
    """一次刮削的检索状态, 不随工厂缓存的爬虫实例共享."""

    unlock_attempts: int = 0
    saw_page: bool = False
    last_error: RequestError | None = None


class AmazonCrawler(Crawler):
    """用日文标题补一张高清 DVD 封面, 不提供其它字段."""

    @classmethod
    @override
    def profile(cls) -> CrawlerProfile:
        # 不自定义 Accept-Language: 实测带 ``ja-JP`` 会被亚马逊判到 /age_check/ 年龄门,
        # 用 curl_cffi 指纹自带的请求头才能拿到真实搜索结果页.
        # 结果页直接走 get_text (不走渲染 / 通用拦截启发式): 成人分区的每张结果页都带
        # ``Age Verification`` 弹窗标题, 通用判据会把正常页面当成年龄门.
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
        started = time.monotonic()
        state = _SearchState()
        title = _search_title(query.partial_result, query.number)
        terms = _keywords(title, query.number)
        if not terms:
            return None

        for term in terms:
            hit = await self._lookup(term, title, query.number, state)
            if hit is None:
                continue
            asin, image_id = hit
            # 结果页的成人卡片是懒加载占位图; 真封面从详情页或 ASIN 主图接口补.
            poster_urls = (
                [*_poster_urls(image_id), f"https://m.media-amazon.com/images/P/{asin}.jpg"]
                if image_id
                else await self._cover_urls(asin)
            )
            if not poster_urls:
                continue
            # 与其它来源的 "scrape ok" 对齐: 日志里能直接看到日亚命中了哪一条、用的是原图还是兜底图.
            self.logger.info(
                "scrape ok",
                asin=asin,
                number=query.number,
                cover="original" if image_id else "main-image",
                duration_s=round(time.monotonic() - started, 2),
            )
            return MediaMetadata(
                number=query.number,
                poster_urls=poster_urls,
                source_url=f"{SITE}/dp/{asin}",
                external_id=asin,
            )
        if state.last_error is not None:
            raise state.last_error
        if state.saw_page:
            # 结果页拿到了却没有可用候选: 这个番号在日亚根本没有在售条目, 不是抓取故障.
            # 用独立原因上报, 通知与统计据此区分「站点没卖」和「抓取坏了」.
            raise SourceError(FailureReason.NOT_LISTED, detail=query.number)
        return None

    async def _lookup(self, term: str, title: str, number: str, state: _SearchState) -> tuple[str, str] | None:
        """用一个检索词搜一次结果页, 返回 (asin, 图片 ID).

        先走移动版: 成人条目在那里直接给真封面, 没有黑幕占位图. 移动版没命中才回桌面版,
        桌面版要处理黑幕 (点一次「Yes」并把跳回来的正文直接吃掉).
        """
        for mobile in (True, False):
            url = (_MOBILE_SEARCH_URL if mobile else _SEARCH_URL).format(term=quote(term, safe=""))
            try:
                html = await self._fetch_page(url)
            except RequestError as exc:
                state.last_error = exc
                continue
            if html is None:
                continue
            state.saw_page = True
            if not mobile and _CURTAIN_RE.search(html) and state.unlock_attempts < _UNLOCK_LIMIT:
                # 成人区的卡片在年龄门未确认前只给 60x40 占位图. 资格跟着这一次请求链走,
                # 单靠 cookie 落地再重取并不可靠 (实测重取回来还是占位图), 所以先吃跳回来的正文.
                state.unlock_attempts += 1
                unlocked = await self._unlock(url)
                if unlocked is not None and _parse_results(unlocked):
                    html = unlocked
                else:
                    try:
                        html = await self._fetch_page(url)
                    except RequestError as exc:
                        state.last_error = exc
                        continue
                    if html is None:
                        continue
            hit = self._best(html, term, title, number)
            if hit is not None:
                return hit
        return None

    def _best(self, html: str, term: str, title: str, number: str) -> tuple[str, str] | None:
        """一页结果里挑最像的那条; 没有可用的返回 None."""
        results = _parse_results(html)
        best: tuple[float, str, str] | None = None
        for asin, image_id, found in results:
            score = _match_score(number, title, found)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, asin, image_id)
        self.logger.debug("amazon search", term=term, candidates=len(results))
        if best is None:
            return None
        _score, asin, image_id = best
        return asin, image_id

    async def _fetch_page(self, url: str) -> str | None:
        """读取页面正文; 年龄门按预算重试, 网络错误交由检索入口尝试下一地址."""
        for attempt in range(1, _MAX_SEARCH_ATTEMPTS + 1):
            try:
                # 故意用 get_text 而不是 get_html: 通用拦截启发式把结果页里的 "Age Verification"
                # 弹窗标题当成年龄门 (成人分区每页都有那个 black-curtain 弹窗), 会把正常页面判死.
                html = await self.client.get_text(url, cookies=self.cookies or None)
            except RequestError as exc:
                self.logger.debug("amazon search failed", error=str(exc))
                raise
            if not _GATE_RE.search(html):
                return html
            # 真正的年龄门 (/age_check/) 是概率性的: 同一 URL 换一次请求往往就正常,
            # 用尽次数后照常上报, 不当成"没搜到".
            if attempt == _MAX_SEARCH_ATTEMPTS:
                raise SourceError(FailureReason.AGE_VERIFICATION, detail=url)
            self.logger.debug("amazon age gate, retry", attempt=attempt, url=url)
        return None

    async def _unlock(self, return_url: str) -> str | None:
        """点一次黑幕弹窗的「Yes」, 返回跳回的结果页正文 (失败返回 None)."""
        url = _ELIGIBILITY_URL.format(quote(return_url, safe=""))
        try:
            text = await self.client.get_text(url, cookies=self.cookies or None)
        except RequestError as exc:
            self.logger.debug("amazon eligibility failed", error=str(exc))
            return None
        self.logger.debug("amazon black curtain unlocked", url=return_url)
        return text

    async def _cover_urls(self, asin: str) -> list[str]:
        """结果页只有占位图时的候选封面: 详情页原图优先, 退到按 ASIN 直取主图."""
        image_id = await self._detail_image(asin)
        if image_id:
            return _poster_urls(image_id)
        # 详情页现在常回 JS 空壳 (200 但无 data-old-hires); 按 ASIN 取主图这条总是有货,
        # 实测 352x500 / 28 KB —— 比没有封面强, 但比真封面小得多.
        # 再补一条按 ASIN 镜像日亚封面的地址 (tenhow, 实测 1055x1500):
        # 管线下载完按像素面积取最大, 镜像没收录该 ASIN 时 404、下载失败会被自然跳过.
        base = "https://m.media-amazon.com/images/P"
        return [
            f"{base}/{asin}.jpg",
            f"{base}/{asin}.01._SCLZZZZZZZ_.jpg",
            f"https://tenhow.net/images/{asin}.jpg",
        ]

    async def _detail_image(self, asin: str) -> str | None:
        """详情页取真封面 ID: ``data-old-hires`` → 主图 JSON → ``landingImage`` 的 src.

        详情页有时回 200 的 JS 空壳 (一个图像字段都没有): 换入口再试, 全空才放弃.
        """
        for path in _DETAIL_PATHS:
            try:
                html = await self._fetch_page(f"{SITE}{path.format(asin=asin)}")
            except RequestError:
                continue
            if html is None:
                continue
            image_id = _image_from_detail(html)
            if image_id:
                return image_id
        self.logger.debug("amazon detail page has no cover", asin=asin)
        return None


def _image_from_detail(html: str) -> str | None:
    """仅从商品主图及主图库取图, 不采用推荐商品或背面图片."""
    page = Selector(text=html)
    main_nodes = page.css("#landingImage, #imgBlkFront, #main-image")
    for node in main_nodes:
        if image := _image_on_node(node, original_only=True):
            return image
    for script_node in page.css("script"):
        if script_node.xpath(
            "ancestor::*[contains(@id, 'recommend') or contains(@id, 'similarities') or "
            "contains(@id, 'sponsored') or contains(@id, 'desktop-dp-sims')]"
        ):
            continue
        script = script_node.xpath("text()").get("")
        for match in _GALLERY_RE.finditer(script):
            try:
                entries, _ = json.JSONDecoder().raw_decode(script[match.end() :])
            except ValueError, RecursionError:
                continue
            if not isinstance(entries, list) or not entries:
                continue
            main = next(
                (entry for entry in entries if isinstance(entry, dict) and entry.get("variant") == "MAIN"), None
            )
            # 未标注视角的旧图库只有单张时才可以确认是主图.
            if main is None and len(entries) == 1 and isinstance(entries[0], dict) and not entries[0].get("variant"):
                main = entries[0]
            if main is None:
                continue
            for key in ("hiRes", "large"):
                url = main.get(key)
                if isinstance(url, str) and (image := _image_id(url)):
                    return image
    for node in main_nodes:
        if image := _image_on_node(node):
            return image
    return None
