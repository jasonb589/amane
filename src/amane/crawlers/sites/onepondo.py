"""一本道公开 JSON 目录; 影片标识和公开剧照必须属于请求的目录号.

官网只有横版封面, 竖版海报由图片管线裁剪. 会员剧照与正片地址不进入输出.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from ...enums import ActorGender, SiteName
from ...net.connectivity import ConnectivityOutcome, probe_get
from ...net.errors import FailureReason, SourceError, parse_detail
from ...parsing.file_info import ContentType, normalize_onepondo_number
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, ResponseModel, SearchQuery, film_actors

_BASE_URL = "https://www.1pondo.tv"
_DETAIL_PATH = re.compile(r"/(?:movies/([0-9]{6}_[0-9]{3})/?|moviepages/([0-9]{6}_[0-9]{3})/(?:index\.html)?)")
_IMAGE_FILE = r"[A-Za-z0-9_-]+\.(?:jpe?g|png|webp)"
_SAMPLE_FILE = re.compile(r"([0-9]{3,4})p\.mp4")


class _Sample(ResponseModel):
    filename: str = Field(default="", alias="FileName")
    url: str = Field(default="", alias="URL")


class _Movie(ResponseModel):
    number: str = Field(default="", alias="MovieID")
    title: str = Field(default="", alias="Title")
    plot: str | None = Field(default=None, alias="Desc")
    actresses: list[str] = Field(default_factory=list, alias="ActressesJa")
    actors: str = Field(default="", alias="Actor")
    tags: list[str] = Field(default_factory=list, alias="UCNAME")
    series: str | None = Field(default=None, alias="Series")
    release: str | None = Field(default=None, alias="Release")
    duration: int | None = Field(default=None, alias="Duration")
    score: float | None = Field(default=None, alias="AvgRating")
    meta_id: int | None = Field(default=None, alias="MetaMovieID")
    gallery: bool = Field(default=False, alias="Gallery")
    legacy_gallery: bool = Field(default=False, alias="HasGallery")
    thumb_ultra: str = Field(default="", alias="ThumbUltra")
    thumb_high: str = Field(default="", alias="ThumbHigh")
    thumb_medium: str = Field(default="", alias="ThumbMed")
    thumb_low: str = Field(default="", alias="ThumbLow")
    thumb: str = Field(default="", alias="MovieThumb")
    samples: list[_Sample] = Field(default_factory=list, alias="SampleFiles")


class _GalleryRow(ResponseModel):
    number: str = Field(default="", alias="MovieID")
    filename: str = Field(default="", alias="Filename")
    image: str = Field(default="", alias="Img")
    # 缺少访问标记的条目不能作为公开剧照输出.
    protected: bool = Field(default=True, alias="Protected")


class _Gallery(ResponseModel):
    number: str = Field(default="", alias="MovieID")
    meta_id: int | None = Field(default=None, alias="MetaMovieID")
    rows: list[_GalleryRow] = Field(default_factory=list, alias="Rows")


def _http_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


class OnePondoCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.ONEPONDO, base_url=_BASE_URL, same_origin_referer=True)

    def _headers(self) -> dict[str, str]:
        return {"Referer": f"{self.base_url}/", **self.headers}

    def _movie_url(self, number: str) -> str:
        return f"{self.base_url}/movies/{number}/"

    def _url_number(self, url: str) -> str | None:
        if _http_url(url) is None:
            return None
        parsed = urlsplit(url)
        for entry in (urlsplit(self.base_url), urlsplit(_BASE_URL)):
            if parsed.netloc.lower().removeprefix("www.") != entry.netloc.lower().removeprefix("www."):
                continue
            prefix = entry.path.rstrip("/")
            if prefix and not parsed.path.startswith(f"{prefix}/"):
                continue
            match = _DETAIL_PATH.fullmatch(parsed.path[len(prefix) :])
            if match is not None:
                return normalize_onepondo_number(match[1] or match[2])
        return None

    def serves_url(self, url: str) -> bool:
        return self._url_number(url) is not None

    async def check_connectivity(self) -> ConnectivityOutcome:
        # JSON 获取始终使用 HTTP; 探测也不随 HTML 浏览器策略改变请求形态.
        return await probe_get(
            self.client.web_client,
            f"{self.base_url}/dyn/phpauto/movie_lists/list_newest_0.json",
            cookies=self.cookies,
            headers=self._headers(),
        )

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        if query.content_type not in {None, ContentType.UNCENSORED}:
            return None
        number = normalize_onepondo_number(query.number)
        return self._movie_url(number) if number is not None else None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        number = self._url_number(url)
        if number is None:
            return None
        endpoint = f"{self.base_url}/dyn/phpauto/movie_details/movie_id/{number}.json"
        try:
            data = await self.client.get_json(endpoint, cookies=self.cookies, headers=self._headers())
        except SourceError as exc:
            if exc.reason is FailureReason.NOT_FOUND:
                return None
            raise
        try:
            movie = _Movie.model_validate(data)
        except ValidationError as exc:
            raise SourceError(FailureReason.PARSE_ERROR, detail=parse_detail(exc), url=endpoint) from exc
        if not movie.number:
            raise SourceError(FailureReason.PARSE_ERROR, detail="missing 1Pondo movie ID", url=endpoint)
        if movie.number != number:
            return None
        if not movie.title.strip():
            raise SourceError(FailureReason.PARSE_ERROR, detail="missing 1Pondo title", url=endpoint)
        images = self._covers(movie)
        actors = film_actors(dict.fromkeys(name.strip() for name in movie.actresses if name.strip()))
        if not actors:
            actors = film_actors(
                dict.fromkeys(name.strip() for name in re.split(r"[,、]", movie.actors) if name.strip()),
                gender=ActorGender.UNKNOWN,
            )
        return MediaMetadata(
            number=number,
            title=movie.title,
            plot=movie.plot,
            actors=actors,
            studio="一本道",
            series=movie.series,
            release=movie.release,
            runtime=(movie.duration + 59) // 60 if movie.duration is not None and movie.duration > 0 else None,
            score=movie.score if movie.score is not None and 0 <= movie.score <= 5 else None,
            tags=list(dict.fromkeys(movie.tags)),
            thumb_urls=images,
            poster_urls=images,
            extrafanart=await self._gallery(movie),
            trailer_urls=self._trailers(movie),
            external_id=str(movie.meta_id) if movie.meta_id is not None else None,
            source_url=self._movie_url(number),
        )

    def _cover_url(self, value: str, number: str) -> str | None:
        if _http_url(value) is None:
            return None
        parsed = urlsplit(value)
        for entry in (urlsplit(self.base_url), urlsplit(_BASE_URL)):
            if parsed.netloc.lower().removeprefix("www.") != entry.netloc.lower().removeprefix("www."):
                continue
            prefix = entry.path.rstrip("/")
            if prefix and not parsed.path.startswith(f"{prefix}/"):
                continue
            path = parsed.path[len(prefix) :]
            if re.fullmatch(rf"/(?:moviepages/{number}/images|assets/sample/{number})/{_IMAGE_FILE}", path):
                return f"{self.base_url}{path}" + (f"?{parsed.query}" if parsed.query else "")
        return None

    def _covers(self, movie: _Movie) -> list[str]:
        images = [
            image
            for value in (movie.thumb_ultra, movie.thumb_high, movie.thumb_medium, movie.thumb_low)
            if (image := self._cover_url(value, movie.number)) is not None
        ]
        # 该地址来自官网播放器配置, 与 API 的旧目录路径互为备选, 不扩展到其它目录号.
        images.append(f"{self.base_url}/assets/sample/{movie.number}/str.jpg")
        if image := self._cover_url(movie.thumb, movie.number):
            images.append(image)
        return list(dict.fromkeys(images))

    def _trailers(self, movie: _Movie) -> list[str]:
        samples: list[tuple[int, str]] = []
        for sample in movie.samples:
            match = _SAMPLE_FILE.fullmatch(sample.filename)
            if match is None or _http_url(sample.url) is None:
                continue
            parsed = urlsplit(sample.url)
            host = parsed.hostname or ""
            if host != "1pondo.tv" and not host.endswith(".1pondo.tv"):
                continue
            if parsed.path != f"/sample/movies/{movie.number}/{sample.filename}":
                continue
            samples.append((int(match[1]), sample.url))
        return list(dict.fromkeys(url for _, url in sorted(samples, key=lambda pair: pair[0], reverse=True)))

    async def _gallery(self, movie: _Movie) -> list[str]:
        if not (movie.gallery or movie.legacy_gallery):
            return []
        path = (
            f"/dyn/dla/json/movie_gallery/{movie.number}.json"
            if movie.gallery
            else f"/dyn/phpauto/movie_galleries/movie_id/{movie.number}.json"
        )
        endpoint = f"{self.base_url}{path}"
        try:
            data = await self.client.get_json(endpoint, cookies=self.cookies, headers=self._headers())
            gallery = _Gallery.model_validate(data)
        except (SourceError, ValidationError) as exc:
            # 剧照失败不能废弃已经确认的影片资料; 保留来源日志中的失败原因.
            self.logger.warning(
                "gallery unavailable",
                number=movie.number,
                reason=exc.reason if isinstance(exc, SourceError) else FailureReason.PARSE_ERROR,
            )
            return []
        if movie.gallery:
            if gallery.number != movie.number:
                return []
        elif gallery.meta_id != movie.meta_id:
            return []
        images: list[str] = []
        for row in gallery.rows:
            if row.protected:
                continue
            if movie.gallery:
                if re.fullmatch(rf"movie_gallery/sample/{movie.number}/{_IMAGE_FILE}", row.image):
                    images.append(f"{self.base_url}/dyn/dla/images/{row.image}")
            elif row.number == movie.number and re.fullmatch(r"[0-9]+\.jpg", row.filename):
                images.append(f"{self.base_url}/assets/sample/{movie.number}/popu/{row.filename}")
        return list(dict.fromkeys(images))
