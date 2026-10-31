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

检索词按 ``_keywords()`` 分级回退: **去噪后的整标题 → 逐段砍掉尾部 (厂商 / 女優名) → 番号**。
DMM / FANZA 的商品标题常带促销与渠道噪声 (``【視聴期限なし】`` / ``オンラインコード版`` /
``特典生写真`` / ``送料無料`` …), 原样丢给亚马逊会 0 结果; 而且亚马逊对长检索词几乎按
"全词都得露脸" 处理, 多一个厂商名同样 0 结果 (实测 ``義母 前編 アイデアポケット`` 0 条,
``義母 前編`` 13 条), 所以清洗之后还要从长到短逐级截短。番号只在标题全灭时兜底, 且必须同时
命中碟片标记、不能是特典周边条目 —— 否则宁可返回 None。
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import override
from urllib.parse import quote

from ...enums import SiteName
from ...net.errors import FailureReason, SourceError
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
_MAX_CANDIDATES = 8
_MAX_KEYWORDS = 3
"""检索词最多试几个: 整标题 → 逐段截短, 每个词各自带年龄门重试."""

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
_SEPARATOR_RE = re.compile(r"[|｜/／]+")
_CJK_RE = re.compile("[\u3040-\u30ff\u4e00-\u9fff]")
#: 真正的年龄门: 跳转 ``/age_check/`` 或带 ``driver-verify`` 的表单. 成人分区结果页里那个
#: ``Age Verification`` 只是 18 禁弹窗 (``black-curtain-popover``) 的标题, 不能当拦截判据.
_GATE_RE = re.compile(r'driver-verify|/age_check/|name="agecheck"', re.IGNORECASE)
#: 只靠番号命中时要的碟片标记 / 要排除的周边条目 (同一番号的生写真、ポストカード也占结果位).
_DISC_RE = re.compile(r"DVD|Blu-?ray|ブルーレイ", re.IGNORECASE)
_GOODS_RE = re.compile("特典|生写真|ポストカード|クリアファイル|タオル|抱き枕|非売品|カレンダー|フィギュア")
_NUMBER_ONLY_SCORE = 0.2
"""番号单独命中的底分: 低于任何标题命中, 用来把「不带标题但确实是本片」的结果排在后面."""

_MAX_SEARCH_ATTEMPTS = 3
"""单个检索词的尝试次数: 亚马逊的 /age_check/ 年龄门是概率性的, 换一次请求常常就正常."""


def _clean(title: str) -> str:
    """去掉标题里的括号备注、促销词与分隔符, 只留可以拿去检索的部分."""
    text = _BRACKET_RE.sub(" ", title or "")
    for token in _NOISE_TOKENS:
        text = text.replace(token, " ")
    text = _SEPARATOR_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -‐—·・,，.。:：!")


def _keywords(title: str, number: str) -> list[str]:
    """检索词, 由强到弱: 整标题 → 逐段砍掉尾部 → 番号; 按比对用的归一化形态去重.

    亚马逊对长检索词接近 "全词都要露脸": ``義母 前編 アイデアポケット`` 是 0 条,
    ``義母 前編`` 是 13 条 —— 多带一个厂商名就整页空白. 所以从长到短逐级截短, 但砍掉的
    那一段必须含日文 (纯拉丁标题砍半只会搜到别的卷).
    """
    tokens = _clean(title).split()
    terms: list[str] = []
    seen: set[str] = set()
    for size in range(len(tokens), 1, -1):
        if size < len(tokens) and not _CJK_RE.search(" ".join(tokens[size:])):
            continue
        term = " ".join(tokens[:size])
        norm = _norm(term)
        if len(norm) < 4 or norm in seen:
            continue
        seen.add(norm)
        terms.append(term)
        if len(terms) >= _MAX_KEYWORDS:
            return terms
    number = (number or "").strip()
    if len(terms) < _MAX_KEYWORDS and len(_norm(number)) >= 4 and _norm(number) not in seen:
        terms.append(number)
    return terms


def _norm(text: str) -> str:
    """只留可读字符, 用于标题比对.

    先做 NFKC: 罗马数字 (Ⅷ → VIII)、全角字母/数字/半角假名都是兼容字符, 不折叠就会被当成噪声删掉,
    ``BEAUTY VENUS VIII`` 与亚马逊的 ``BEAUTY VENUS Ⅷ`` 会判成两个标题.
    """
    return _NOISE_RE.sub("", unicodedata.normalize("NFKC", text or "").lower())


def _title_matches(wanted: str, found: str) -> bool:
    """包含即算命中; 否则看相似度 (副标题差异不该判错)."""
    left, right = _norm(wanted), _norm(found)
    if len(left) < 4 or len(right) < 4:
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= _MIN_RATIO


def _match_score(number: str, wanted: str, found: str) -> float:
    """候选结果的可取分数; ``0`` 表示不能用.

    标题命中按相似度计分; 只剩番号命中时给底分, 并且必须带碟片标记、不是特典周边 ——
    同一番号在亚马逊上还有一堆生写真 / ポストカード 条目, 取它们的图就是错封面.
    """
    number_norm = _norm(number)
    found_norm = _norm(found)
    score = 0.0
    if len(number_norm) >= 4 and number_norm in found_norm:
        if not _DISC_RE.search(found) or _GOODS_RE.search(found):
            return 0.0
        score = _NUMBER_ONLY_SCORE
    if _title_matches(wanted, found):
        score += SequenceMatcher(None, _norm(wanted), found_norm).ratio()
    return score


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
        partial = query.partial_result
        title = (partial.title or "").strip() if partial is not None else ""
        if len(_norm(title)) < 4:
            return None

        for term in _keywords(title, query.number):
            hit = await self._lookup(term, title, query.number)
            if hit is None:
                continue
            asin, image_id = hit
            return MediaMetadata(
                number=query.number,
                poster_urls=_poster_urls(image_id),
                source_url=f"{SITE}/dp/{asin}",
                external_id=asin,
            )
        return None

    async def _lookup(self, term: str, title: str, number: str) -> tuple[str, str] | None:
        """用一个检索词搜一次结果页, 返回 (asin, 图片 ID)."""
        url = _SEARCH_URL.format(term=quote(term, safe=""))
        html = ""
        for attempt in range(1, _MAX_SEARCH_ATTEMPTS + 1):
            try:
                # 故意用 get_text 而不是 get_html: 通用拦截启发式把结果页里的 "Age Verification"
                # 弹窗标题当成年龄门 (成人分区每页都有那个 black-curtain 弹窗), 会把正常页面判死.
                html = await self.client.get_text(url, cookies=self.cookies or None)
            except RequestError as exc:
                self.logger.debug("amazon search failed", error=str(exc))
                return None
            if not _GATE_RE.search(html):
                break
            # 真正的年龄门 (/age_check/) 是概率性的: 同一 URL 换一次请求往往就正常,
            # 用尽次数后照常上报, 不当成"没搜到".
            if attempt == _MAX_SEARCH_ATTEMPTS:
                raise SourceError(FailureReason.AGE_VERIFICATION, detail=url)
            self.logger.debug("amazon age gate, retry", attempt=attempt, url=url)

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
