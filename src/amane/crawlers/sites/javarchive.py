"""JavArchive 的 FC2 条目: 检索与正文分别核对商品 ID, 图片仅采用主正文声明的地址."""

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

from parsel import Selector

from ...enums import SiteName
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery
from ..parsing import fc2_article_id, fc2_number

_DETAIL_PATH = re.compile(r"^/\d+-FC2(?:[-_]?PPV)?[-_]?(?P<article>[0-9]+)-.+-pn\.html$", re.IGNORECASE)
_TITLE_NUMBER = re.compile(r"^FC2(?:[-_\s]*PPV)?[-_\s]*(?P<article>[0-9]+)(?![0-9A-Za-z_])", re.IGNORECASE)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


@dataclass(frozen=True)
class _ArticleLink:
    url: str
    article_id: str


class JavArchiveCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(
            name=SiteName.JAVARCHIVE, base_url="https://javarchive.com", urls=["https://javarchive.com"]
        )

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        article_id = fc2_article_id(query.number)
        if article_id is None:
            return None
        url = f"{self.base_url}/search?{urlencode({'q': fc2_number(article_id, ppv=True)})}"
        html = Selector(text=await self.client.get_html(url, cookies=self.cookies, headers=self.headers))
        # 空检索同样返回热门条目; 侧栏和推荐区不参与番号匹配.
        for anchor in html.css(".box_right_all > .news_1n > ul > li h3 a"):
            title = anchor.xpath("string(.)").get("").strip()
            title_number = _TITLE_NUMBER.match(title)
            if title_number is None or title_number["article"] != article_id:
                continue
            link = self._article_link(anchor.attrib.get("href", ""))
            if link is not None and link.article_id == article_id:
                return link.url
        return None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        link = self._article_link(url)
        if link is None:
            return None
        html = Selector(text=await self.client.get_html(link.url, cookies=self.cookies, headers=self.headers))
        main = html.css(".box_right_all > .category_news_phai_chinh")
        if len(main) != 1:
            return None
        headings = main.xpath('./*[contains(concat(" ", normalize-space(@class), " "), " menudd ")]/h1')
        if len(headings) != 1:
            return None
        raw_title = headings[0].xpath("string(.)").get("").strip()
        title_number = _TITLE_NUMBER.match(raw_title)
        if title_number is None or title_number["article"] != link.article_id:
            return None
        title = raw_title[title_number.end() :].lstrip(" \t\r\n-_:：")
        if not title:
            return None
        news = main.xpath('./*[contains(concat(" ", normalize-space(@class), " "), " news ")]')
        if len(news) != 1:
            return None

        # Recipepod 使用食谱 schema 的评分, 页面日期是发布文章的日期, 均不能作为影片字段.
        return MediaMetadata(
            number=fc2_number(link.article_id, ppv=False),
            title=title,
            poster_urls=self._image_urls(
                news.css(':scope > .Recipepod > img[itemprop="image"]::attr(src)').getall(), link.url
            ),
            thumb_urls=self._image_urls(news.css(":scope > .fisrst_sc > img::attr(src)").getall(), link.url),
            extrafanart=self._image_urls(news.css(":scope > #lightgallery > li::attr(data-src)").getall(), link.url),
            source_url=link.url,
        )

    def _article_link(self, href: str) -> _ArticleLink | None:
        """官方链接与镜像链接统一指向配置入口; 禁止外站链接取得配置 cookie."""
        href = href.strip()
        if not href or any(char in href for char in "\\\r\n\t"):
            return None
        try:
            base = urlsplit(self.base_url)
            parsed = urlsplit(href)
            if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
                return None
            if parsed.scheme and parsed.scheme not in {"http", "https"}:
                return None
            if parsed.username is not None or parsed.password is not None:
                return None
            if parsed.netloc:
                host = (parsed.hostname or "").lower().rstrip(".").removeprefix("www.")
                base_host = base.hostname.lower().rstrip(".").removeprefix("www.")
                if host not in {base_host, "javarchive.com"}:
                    return None
                if parsed.port is not None and parsed.port != base.port and parsed.port not in {80, 443}:
                    return None
            elif parsed.scheme:
                return None
        except ValueError:
            return None

        prefix = base.path.rstrip("/")
        path = "/" + parsed.path.lstrip("/")
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        decoded = unquote(path)
        if any(segment in {".", ".."} for segment in decoded.split("/")) or any(char in decoded for char in "\\\r\n\t"):
            return None
        match = _DETAIL_PATH.fullmatch(decoded)
        if match is None:
            return None
        # 标题 slug 可能包含斜杠; 只校验文章前缀和结尾, 保留站点给出的原始编码.
        return _ArticleLink(
            url=urlunsplit((base.scheme, base.netloc, prefix + path, "", "")), article_id=match["article"]
        )

    def _image_urls(self, values: list[str], page_url: str) -> list[str]:
        urls: list[str] = []
        for value in values:
            if not value.strip() or any(char in value for char in "\\\r\n\t"):
                continue
            try:
                value = value.strip()
                if value.startswith("/") and not value.startswith("//"):
                    base = urlsplit(self.base_url)
                    relative = urlsplit(value)
                    prefix = base.path.rstrip("/")
                    path = relative.path
                    if prefix and not path.startswith(prefix + "/"):
                        path = prefix + path
                    value = urlunsplit((base.scheme, base.netloc, path, relative.query, ""))
                parsed = urlsplit(urljoin(page_url, value))
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or not unquote(parsed.path).lower().endswith(_IMAGE_SUFFIXES)
                ):
                    continue
            except ValueError:
                continue
            url = urlunsplit(parsed._replace(fragment=""))
            if url not in urls:
                urls.append(url)
        return urls
