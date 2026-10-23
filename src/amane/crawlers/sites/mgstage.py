import re

from parsel import Selector

from ...enums import SiteName
from ...parsing.file_info import ContentType
from ..base import Crawler, CrawlerProfile
from ..http import RequestError
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors
from ..parsing import extract_all_texts, extract_text, number_variants, select_number_index

#: 竖版海报与横版放大图同目录同名, 只差文件名前缀; 详情页只引用后者, 海报地址由封面推导.
_COVER_PREFIX = "pb_e_"
_POSTER_PREFIX = "pf_e_"
#: 素人条目的出演栏写的是站点的代名 (名字后跟年龄与职业), 不是可对齐的演员身份.
_AMATEUR_TAG = "素人"


def _poster_url(cover_url: str) -> str | None:
    """横版封面地址 → 同目录竖版海报地址; 非 ``pb_e_`` 形态返回 ``None``."""
    directory, _, filename = cover_url.rpartition("/")
    if not directory or not filename.startswith(_COVER_PREFIX):
        return None
    return f"{directory}/{_POSTER_PREFIX}{filename.removeprefix(_COVER_PREFIX)}"


def _wide_cover(cover_url: str) -> str | None:
    """任意 ``pb_*`` / ``pf_*`` 封面地址 → 同图的 ``pb_e_`` 展开大图 (840x473); 推不出返回 ``None``.

    素人页面主图可能是 ``pb_p_`` (540x304 小横版) 或 ``pf_o1_`` (166x236 小竖版), 两种前缀都指向
    同一张图; 展开版 ``pb_e_`` 才是高清背景图/封面该用的那张. 只认 ``/pf_`` 会让主图是 ``pb_p_``
    的条目永远停在 540x304.
    """
    if not cover_url:
        return None
    wide = re.sub(r"/(?:pf|pb)_[a-z0-9]+_", f"/{_COVER_PREFIX}", cover_url)
    return wide if wide != cover_url else None


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

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        """素人条目额外带上页面唯一的横版图 (pb_e): 竖版原生封面排在海报候选首位, 落盘时不再
        拿封面裁一份海报; 横版图随后被当作缩略图 / 背景图使用.

        横版候选只在这里产出 (content_type = 素人); 其它来源与内容类型不带该字段, 落盘行为不变.
        """
        meta = await super().fetch(query, options)
        if meta is None or query.content_type is not ContentType.AMATEUR or meta.poster_urls:
            return meta
        cover = meta.thumb_urls[0] if meta.thumb_urls else ""
        if not cover:
            return meta
        # 展开大图 ``pb_e_`` 是同一张图的 840x473 版本 (正面+书脊+封底), 两种主图前缀都要能推.
        wide = _wide_cover(cover)
        if wide is None:
            return meta
        return meta.model_copy(
            update={
                # 海报候选首个仍是原图 (竖版 pf_e_ 在 _scrape 里已单独推导), 大横图排后面备用.
                "poster_urls": [cover, wide],
                # 封面直接用大横图, 背景图/裁切都以它为准.
                "thumb_urls": [wide, cover],
            }
        )

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

        # 封面: 主图可能是竖版小图 pf_o1_ 或横版 pb_p_/pb_e_; 放大图指向 pb_e_ (840x473 横版),
        # 同目录下都有竖版大封面 pf_e_ (422x600), 页面不直接引用, 因此改写前缀推导.
        cover = extract_text(html, '//a[@id="EnlargeImage"]/@href', '//img[@class="enlarge_image"]/@src')
        thumb_url = cover or None
        poster_url = _poster_url(cover)
        if poster_url is None:
            # 上游 _poster_url 只认 pb_e_; 主图是竖版小图时再按前缀推导一次.
            for src in (extract_text(html, '//img[@class="enlarge_image"]/@src'), cover):
                if not src:
                    continue
                derived = re.sub(r"/(?:pf|pb)_[a-z0-9]+_", "/pf_e_", src)
                if derived and derived != src:
                    poster_url = derived
                    break

        extrafanart = extract_all_texts(
            html,
            '//dl[@id="sample-photo"]//li/a/@href',
            '//*[@id="sample-photo"]//a[@class="sample_image"]/@href',
            '//ul[@id="sample-photo"]/li/a/@href',
        )

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
            poster_urls=[poster_url] if poster_url else [],
            thumb_urls=[thumb_url] if thumb_url else [],
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
