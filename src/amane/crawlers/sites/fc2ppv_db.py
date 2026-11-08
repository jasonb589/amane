"""FC2PPV-DB 的详情页包含 RSC 影片对象; 页面番号与对象 ID 必须同时匹配请求.

详情 URL 由 FC2 商品 ID 确定, ``fetch`` 只请求一次页面. 元数据仅来自主影片对象, 不读取推荐区.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from parsel import Selector
from pydantic import Field, ValidationError

from ...enums import SiteName
from ...net.connectivity import ConnectivityOutcome
from ...net.errors import FailureReason, SourceError, parse_detail
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, ResponseModel, SearchQuery, film_actors
from ..parsing import fc2_article_id, fc2_number

_FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[\s*1\s*,\s*("(?:[^"\\]|\\.)*")\s*\]\)')
_VIDEO_PROP = re.compile(r'"video"\s*:\s*(?=\{)')
_HEADING_NUMBER = re.compile(r"^(FC2(?:[\s_-]*PPV)?[\s_-]*[0-9]+)(?=\s|$)", re.IGNORECASE)
_DETAIL_PATH = re.compile(r"/(?:ja|en|zh)/videos/([0-9]+)/?")
_CDN_BASE = "https://d39jz7pbpqkw9s.cloudfront.net"


class _Named(ResponseModel):
    name: str = ""


class _Actress(ResponseModel):
    video_id: str | None = Field(default=None, alias="videoId")
    actress: _Named | None = None
    status: str | None = None


class _Tag(ResponseModel):
    video_id: str | None = Field(default=None, alias="videoId")
    tag: _Named | None = None


class _Image(ResponseModel):
    video_id: str | None = Field(default=None, alias="videoId")
    image_url: str | None = Field(default=None, alias="imageUrl")
    image_local: str | None = Field(default=None, alias="imageLocal")
    sort_order: int = Field(default=0, alias="sortOrder")


class _Video(ResponseModel):
    id: str
    title: str
    description: str | None = None
    thumbnail_url: str | None = Field(default=None, alias="thumbnailUrl")
    thumbnail_local: str | None = Field(default=None, alias="thumbnailLocal")
    release: str | None = Field(default=None, alias="releaseDate")
    duration: int | None = None
    seller: _Named | None = None
    images: list[_Image] = Field(default_factory=list)
    actresses: list[_Actress] = Field(default_factory=list)
    tags: list[_Tag] = Field(default_factory=list, alias="productTags")
    rating: float | None = Field(default=None, alias="avgRating")
    rating_count: int = Field(default=0, alias="ratingCount")


class FC2PPVDBIndexCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(
            name=SiteName.FC2PPV_DB,
            base_url="https://fc2ppv-db.com",
            urls=["https://fc2ppv-db.com"],
            cookies={"age-verified": "true"},
        )

    def _detail_url(self, number: str) -> str | None:
        article_id = fc2_article_id(number)
        if article_id is None:
            return None
        return f"{self._entry_url()}/videos/{article_id}"

    def _entry_url(self) -> str:
        base = self.base_url.rstrip("/")
        locale = "" if urlsplit(base).path.rsplit("/", 1)[-1] in {"ja", "en", "zh"} else "/ja"
        return f"{base}{locale}"

    async def check_connectivity(self) -> ConnectivityOutcome:
        return await self.client.check(self._entry_url(), cookies=self.cookies, headers=self.headers)

    def _article_id(self, url: str) -> str | None:
        try:
            parsed = urlsplit(url)
            configured = urlsplit(self.base_url)
            official = urlsplit(self._profile.base_url)
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        host = parsed.netloc.lower().removeprefix("www.")
        for candidate in (configured, official):
            if host != candidate.netloc.lower().removeprefix("www."):
                continue
            prefix = candidate.path.rstrip("/")
            if prefix and not parsed.path.startswith(prefix + "/"):
                continue
            path = parsed.path[len(prefix) :]
            locale = prefix.rsplit("/", 1)[-1]
            if locale in {"ja", "en", "zh"}:
                path = f"/{locale}{path}"
            if match := _DETAIL_PATH.fullmatch(path):
                return match.group(1)
        return None

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        url = self._detail_url(query.number)
        return await self._scrape(url, options) if url else None

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        url = self._detail_url(query.number)
        return url if url and await self._scrape(url, options) is not None else None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        article_id = self._article_id(url)
        if article_id is None:
            return None
        # 官方链接按配置入口读取, 确保镜像前缀、语言目录与身份凭据使用同一来源.
        url = f"{self._entry_url()}/videos/{article_id}"
        try:
            text = await self.client.get_html(url, cookies=self.cookies, headers=self.headers)
        except SourceError as exc:
            if exc.reason is FailureReason.NOT_FOUND:
                return None
            raise

        html = Selector(text=text)
        heading = html.css("main h1").xpath("string(.)").get("").strip()
        if heading in {"年齢確認", "Age Verification", "年龄验证"} or "年齢確認" in html.css("title::text").get(""):
            raise SourceError(FailureReason.AGE_VERIFICATION, detail=url, url=url)
        number = _HEADING_NUMBER.match(heading)
        if number is None or fc2_article_id(number.group(1)) != article_id:
            return None
        video, flight = _read_video(text, article_id, url)
        return _metadata(video, flight, url)


def _read_video(text: str, article_id: str, url: str) -> tuple[_Video, str]:
    chunks: list[str] = []
    for chunk in _FLIGHT_CHUNK.finditer(text):
        try:
            chunks.append(json.loads(chunk.group(1)))
        except json.JSONDecodeError:
            continue
    flight = "".join(chunks)
    decoder = json.JSONDecoder()
    for anchor in _VIDEO_PROP.finditer(flight):
        try:
            value, _ = decoder.raw_decode(flight, anchor.end())
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("id") != article_id:
            continue
        try:
            return _Video.model_validate(value), flight
        except ValidationError as exc:
            raise SourceError(FailureReason.PARSE_ERROR, detail=parse_detail(exc), url=url) from exc
    raise SourceError(FailureReason.PARSE_ERROR, detail="missing matching video object", url=url)


def _description(value: str | None, flight: str) -> str | None:
    if not value:
        return None
    if not re.fullmatch(r"\$[0-9a-f]+", value):
        return value.strip() or None
    row = re.search(rf"(?:^|\n){re.escape(value[1:])}:", flight)
    if row is None:
        return None
    payload = flight[row.end() :]
    length = re.match(r"T([0-9a-f]+),", payload)
    if length:
        # RSC 文本长度按 UTF-8 字节计算; 文本内部允许换行与类似记录编号的内容.
        size = int(length.group(1), 16)
        raw = payload[length.end() :].encode("utf-8")
        if len(raw) < size:
            return None
        try:
            return raw[:size].decode("utf-8").strip() or None
        except UnicodeDecodeError:
            return None
    try:
        resolved, _ = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError:
        return None
    return resolved.strip() or None if isinstance(resolved, str) else None


def _http_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("//"):
        value = f"https:{value}"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password):
        return value
    return None


def _image_urls(original: str | None, local: str | None) -> list[str]:
    candidates = [_http_url(original)]
    if local and not local.startswith(("/", ".")) and ":" not in local and ".." not in local.split("/"):
        candidates.append(_http_url(f"{_CDN_BASE}/{local}"))
    return list(dict.fromkeys(value for value in candidates if value))


def _metadata(video: _Video, flight: str, url: str) -> MediaMetadata:
    posters = _image_urls(video.thumbnail_url, video.thumbnail_local)
    images = [
        urls
        for image in sorted(video.images, key=lambda item: item.sort_order)
        if image.video_id in {None, video.id} and (urls := _image_urls(image.image_url, image.image_local))
    ]
    actors = dict.fromkeys(
        name
        for relation in video.actresses
        if relation.video_id in {None, video.id}
        and relation.status in {None, "APPROVED"}
        and relation.actress is not None
        and (name := relation.actress.name.strip())
    )
    tags = dict.fromkeys(
        name
        for relation in video.tags
        if relation.video_id in {None, video.id} and relation.tag is not None and (name := relation.tag.name.strip())
    )
    return MediaMetadata(
        number=fc2_number(video.id, ppv=False),
        title=video.title.strip() or None,
        actors=film_actors(actors),
        studio=video.seller.name.strip() or None if video.seller else None,
        release=video.release.removeprefix("$D") if video.release else None,
        runtime=max(1, round(video.duration / 60)) if video.duration and video.duration > 0 else None,
        tags=list(tags),
        plot=_description(video.description, flight),
        poster_urls=posters,
        thumb_urls=images[0] if images else posters,
        extrafanart=list(dict.fromkeys(urls[0] for urls in images)),
        score=video.rating * 20
        if video.rating_count > 0 and video.rating is not None and 0 <= video.rating <= 5
        else None,
        external_id=video.id,
        source_url=url,
    )
