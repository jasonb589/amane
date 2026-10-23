import re

from parsel import Selector

from ...enums import SiteName
from ..base import Crawler, CrawlerProfile
from ..http import RequestError
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors
from ..parsing import extract_all_texts, extract_text, number_variants, select_number_index

#: 素人条目的出演栏写的是站点的代名 (名字后跟年龄与职业), 不是可对齐的演员身份.
_AMATEUR_TAG = "素人"

#: 横版展开图与竖版海报同目录同名, 只差前缀.
_COVER_PREFIX = "pb_e_"
_POSTER_PREFIX = "pf_e_"


def _poster_url(cover_url: str) -> str | None:
    """横版展开图 → 同目录竖版海报; 非 ``pb_e_`` 形态返回 ``None``.

    正式抓取已改走 :func:`image_urls` (直接按目录拼三种尺寸, 不再依赖页面主图前缀);
    这里保留是为了兼容上游用例 ``tests/crawlers/test_mgstage.py``.
    """
    directory, _, filename = cover_url.rpartition("/")
    if not directory or not filename.startswith(_COVER_PREFIX):
        return None
    return f"{directory}/{_POSTER_PREFIX}{filename.removeprefix(_COVER_PREFIX)}"

#: 页面上任意一张图: 同一条目所有图都在同一个目录下, 文件名只差尺寸前缀.
_IMAGE_URL = re.compile(r"https://image\.mgstage\.com/images/[^\"'\s]+\.jpg")

#: 尺寸前缀: ``pb_e_`` 封面 / ``pf_e_`` 海报 / ``pf_o1_`` 小竖版 / ``pb_p_`` 小横版 / ``cap_e_N_`` 剧照.
_IMAGE_PREFIX = re.compile(r"^(?:cap_t1_\d+|cap_e_\d+|pb_e|pb_p|pf_e|pf_o\d+)_")

#: 剧照序号, 用来确定这一部到底有几张 (页面不写总数).
_CAPTURE_INDEX = re.compile(r"/cap_e_(?P<idx>\d+)_[^/]+\.jpg")


def image_dir(page_text: str) -> tuple[str, str] | None:
    """页面里任一图片地址 → (目录, 文件名主干); 取不到返回 ``None``.

    文件名主干形如 ``siro-5755``: 去掉 ``pb_e_`` / ``pf_o1_`` / ``cap_e_0_`` 这类尺寸前缀,
    并统一小写 (站点实际文件名就是全小写).
    """
    for match in _IMAGE_URL.finditer(page_text):
        directory, _, filename = match.group(0).rpartition("/")
        stem = _IMAGE_PREFIX.sub("", filename).removesuffix(".jpg").lower()
        if directory and stem:
            return f"{directory}/", stem
    return None


def capture_count(page_text: str) -> int:
    """剧照张数 = 页面出现的最大序号 + 1; 一张都没有返回 0."""
    indexes = [int(match["idx"]) for match in _CAPTURE_INDEX.finditer(page_text)]
    return max(indexes) + 1 if indexes else 0


def image_urls(page_text: str) -> tuple[list[str], list[str], list[str]]:
    """按目录直接拼三种尺寸, 返回 (封面, 海报, 剧照).

    站点同一部片的所有图只差文件名前缀, 且前缀是固定的:

    * ``pb_e_`` 横版展开图 840x473 → 封面
    * ``pf_e_`` 竖版大图 422x600 → 海报
    * ``cap_e_N_`` 剧照 840x473

    所以**不需要**判断页面主图是哪一个前缀 —— 以前那套判断正是漏掉一整类页面模板
    (主图 ``pb_p_``) 的原因: 拼不出来就是拼不出来, 而不是静默跳过.
    """
    found = image_dir(page_text)
    if found is None:
        return [], [], []
    base, stem = found
    thumbs = [f"{base}pb_e_{stem}.jpg"]
    posters = [f"{base}pf_e_{stem}.jpg"]
    fanart = [f"{base}cap_e_{index}_{stem}.jpg" for index in range(capture_count(page_text))]
    return thumbs, posters, fanart


class MGStageCrawler(Crawler):
    #: 详情页地址里的番号段, 检索结果与直达地址共用同一形态.
    _DETAIL_PATH = re.compile(r"/product/product_detail/([^/]+)/")

    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.MGSTAGE, base_url="https://www.mgstage.com", cookies={"adc": "1"})

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        """站内品番逐部不同地写不写开头的数字段, 因此按 ``number_variants`` 逐个形态尝试."""
        for number in number_variants(query.number):
            url = await self._search_number(number, query.number)
            if url is not None:
                return url
        return None

    async def _search_number(self, number: str, query_number: str) -> str | None:
        # 先尝试直接访问详情页.
        direct_url = f"{self.base_url}/product/product_detail/{number}/"
        try:
            text = await self.client.get_html(direct_url, cookies=self.cookies)
        except RequestError:
            text = None
        if text:
            html = Selector(text=text)
            title = extract_text(html, '//*[@id="center_column"]/div[1]/h1/text()')
            if title:
                return direct_url

        # 未命中则回退搜索; 结果按条目声明的番号确认, 不取首条.
        search_url = f"{self.base_url}/search/cSearch.php?search_word={number}"
        text = await self.client.get_html(search_url, cookies=self.cookies)
        html = Selector(text=text)
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in html.xpath('//a[contains(@href, "/product/product_detail/")]/@href').getall():
            url = f"{self.base_url}{raw}" if raw.startswith("/") else raw
            match = self._DETAIL_PATH.search(url)
            if match is None or url in seen:
                continue
            seen.add(url)
            candidates.append((url, match[1]))
        index = select_number_index([found for _, found in candidates], query_number)
        return candidates[index][0] if index is not None else None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        text = await self.client.get_html(url, cookies=self.cookies)
        if not text:
            return None

        html = Selector(text=text)

        title = extract_text(html, '//*[@id="center_column"]/div[1]/h1/text()')
        if not title:
            return None

        number_match = re.search(r"/product_detail/([^/]+)", url)
        number = number_match.group(1) if number_match else ""

        tags = extract_all_texts(html, '//th[contains(text(),"ジャンル")]/following-sibling::td/a/text()')

        # 素人条目的出演值是站点代名; 演员字段按首个非空来源锁定, 写入即顶掉 javdb 记的本人, 故不取.
        actors = (
            []
            if any(_AMATEUR_TAG in tag for tag in tags)
            else extract_all_texts(html, '//th[contains(text(),"出演")]/following-sibling::td/a/text()')
        )
        studio = extract_text(html, '//th[contains(text(),"メーカー")]/following-sibling::td/a/text()')
        publisher = extract_text(html, '//th[contains(text(),"レーベル")]/following-sibling::td/a/text()')
        series = extract_text(html, '//th[contains(text(),"シリーズ")]/following-sibling::td/a/text()')

        runtime_raw = extract_text(html, '//th[contains(text(),"収録時間")]/following-sibling::td/text()')
        runtime = self._parse_runtime(runtime_raw)

        release_raw = extract_text(html, '//th[contains(text(),"配信開始日")]/following-sibling::td/text()')
        release = release_raw.strip() if release_raw else None

        # 图片: 三种尺寸都按目录直接拼 (见 image_urls 的说明), 与页面主图是哪个前缀无关.
        thumb_urls, poster_urls, extrafanart = image_urls(text)

        # 商品紹介正文两种标记都收 (直接写在 p.txt.introduction, 或其中的 <p> 被解析器拍平为兄弟节点),
        # 只排除「すべてを見る」.
        plot = "\n".join(
            extract_all_texts(
                html,
                '//dl[@id="introduction"]/dd/p[not(contains(@class, "more"))]//text()',
                '//dl[@id="introduction"]/dd//text()[not(ancestor::p[contains(@class, "more")])]',
            )
        )

        return MediaMetadata(
            number=number,
            title=title,
            actors=film_actors(actors),
            studio=studio or None,
            publisher=publisher or None,
            release=release,
            runtime=runtime,
            tags=tags,
            series=series or None,
            plot=plot or None,
            poster_urls=poster_urls,
            thumb_urls=thumb_urls,
            extrafanart=extrafanart,
            source_url=url,
            external_id=url,
        )

    @staticmethod
    def _parse_runtime(text: str) -> int | None:
        if not text:
            return None
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else None
