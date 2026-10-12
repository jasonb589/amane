import re

from parsel import Selector

from ...enums import SiteName
from ...parsing.file_info import ContentType
from ..base import Crawler, CrawlerProfile
from ..http import RequestError
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors
from ..parsing import extract_all_texts, extract_text


class MGStageCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.MGSTAGE, base_url="https://www.mgstage.com", cookies={"adc": "1"})

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        number = query.number
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

        # 未命中则回退搜索.
        search_url = f"{self.base_url}/search/cSearch.php?search_word={number}"
        text = await self.client.get_html(search_url, cookies=self.cookies)
        html = Selector(text=text)
        results = html.xpath('//a[contains(@href, "/product/product_detail/")]/@href').getall()
        seen: set[str] = set()
        urls: list[str] = []
        for r in results:
            if r.startswith("/"):
                r = self.base_url + r
            if r not in seen:
                seen.add(r)
                urls.append(r)
        return urls[0] if urls else None

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        """素人条目额外带上页面唯一的横版图 (pb_e), 落盘时被当作背景图使用.

        横版候选只在这里产出 (content_type = 素人); 其它来源与内容类型不带该字段, 落盘行为不变.
        """
        meta = await super().fetch(query, options)
        if meta is None or query.content_type is not ContentType.AMATEUR or meta.poster_urls:
            return meta
        cover = next((url for url in meta.thumb_urls if "/pf_" in url), "")
        wide = re.sub(r"/pf_[a-z0-9]+_", "/pb_e_", cover)
        if wide and wide != cover:
            return meta.model_copy(update={"poster_urls": [wide]})
        return meta

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

        actors = extract_all_texts(html, '//th[contains(text(),"出演")]/following-sibling::td/a/text()')
        studio = extract_text(html, '//th[contains(text(),"メーカー")]/following-sibling::td/a/text()')
        publisher = extract_text(html, '//th[contains(text(),"レーベル")]/following-sibling::td/a/text()')
        series = extract_text(html, '//th[contains(text(),"シリーズ")]/following-sibling::td/a/text()')

        runtime_raw = extract_text(html, '//th[contains(text(),"収録時間")]/following-sibling::td/text()')
        runtime = self._parse_runtime(runtime_raw)

        release_raw = extract_text(html, '//th[contains(text(),"配信開始日")]/following-sibling::td/text()')
        release = release_raw.strip() if release_raw else None

        tags = extract_all_texts(html, '//th[contains(text(),"ジャンル")]/following-sibling::td/a/text()')

        # 封面: 页面小图是 pf_o1_ (166x236), "画像を拡大する" 指向 pb_e_ (840x472 横版包装图),
        # 竖版大封面是同目录的 pf_e_ (422x600), 由 pf_o1_ 改写前缀取得; 下载按顺序取第一个成功者.
        cover_thumb = extract_text(html, '//img[@class="enlarge_image"]/@src')
        cover_enlarge = extract_text(html, '//a[@id="EnlargeImage"]/@href')
        thumb_urls: list[str] = []
        if cover_thumb:
            big = re.sub(r"/pf_o1_", "/pf_e_", cover_thumb)
            thumb_urls.append(big)
            if big != cover_thumb:
                thumb_urls.append(cover_thumb)
        if cover_enlarge and cover_enlarge not in thumb_urls:
            thumb_urls.append(cover_enlarge)

        # 剧照: 实际结构是 dl#sample-photo > dd > ul > li > a.sample_image; 旧选择器保留作退路.
        extrafanart = extract_all_texts(
            html,
            '//*[@id="sample-photo"]//a[@class="sample_image"]/@href',
            '//ul[@id="sample-photo"]/li/a/@href',
        )

        # 简介: 文本在 dd 内, class 是 "txt introduction"; 部分版式下解析器会把嵌套 p 拆成兄弟节点,
        # 因此按 dd 收文本, 只排除 "更多" 那一段.
        plot = "\n".join(
            extract_all_texts(
                html,
                '//dl[@id="introduction"]/dd/p[not(contains(@class,"more"))]//text()',
                '//dl[@id="introduction"]/dd//text()[not(ancestor::p[contains(@class,"more")])]',
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
