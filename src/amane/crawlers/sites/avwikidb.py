"""AVWikiDB 的 Next.js 主影片数据; 推荐、评论与相似作品不参与元数据.

标准番号直接读取详情, 缺失时再检索; 检索重定向已经携带详情时不重复请求.
素人演员关系是已识别的演员, ``actorUnknown`` 仅表示原始商品没有公开姓名.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

from pydantic import Field, ValidationError

from ...enums import ActorGender, SiteName
from ...net.connectivity import ConnectivityOutcome
from ...net.errors import FailureReason, SourceError, parse_detail
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, ResponseModel, SearchQuery, film_actors
from ..parsing import NumberMatch, fc2_article_id, next_data_props, number_match, select_number_index
from .dmm_cdn import HIGH_RES_CDN, PICS_CDN, image_candidates

_NUMBER = re.compile(r"(?=.{1,64}$)(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]+(?:-[A-Z0-9]+)*")
_UNDASHED = re.compile(r"(\d*[A-Z]+)(\d{2,}[A-Z]*)")
_CID = re.compile(r"[A-Za-z0-9_-]+")


class _Named(ResponseModel):
    name: str = ""


class _Relation(ResponseModel):
    actor: _Named | None = None
    histrion: _Named | None = None
    director: _Named | None = None
    maker: _Named | None = None
    label: _Named | None = None
    series: _Named | None = None
    genre: _Named | None = None


class _MovieRef(ResponseModel):
    number: str = Field(default="", alias="adultVideoId")
    alias: str | None = Field(default=None, alias="adultVideoAlias")
    mgs_code: str | None = Field(default=None, alias="mgsProductCode")
    floor: str = ""

    @property
    def canonical_number(self) -> str:
        # FANZA 的 MGS SKU 可能另加厂牌前缀; 仅 MGS 目录用完整商品号补缺失别名.
        if self.floor == "mgs":
            return self.alias or self.mgs_code or self.number
        return self.alias or self.number or self.mgs_code or ""


class _Movie(_MovieRef):
    id: int | None = None
    title: str | None = None
    summary: str | None = None
    cid: str | None = Field(default=None, alias="fanzaContentId")
    release: str | None = Field(default=None, alias="dateOfPublication")
    runtime: int | None = Field(default=None, alias="durationMin")
    image_count: int | None = Field(default=None, alias="imageCount")
    large_images: bool = Field(default=False, alias="imageL")
    trailer: str | None = Field(default=None, alias="sampleVideoBestUrl")
    mgs_image: str | None = Field(default=None, alias="mgsImageUrl")
    mgs_samples: list[str] = Field(default_factory=list, alias="mgsSampleImages")
    ambiguous: bool = Field(default=False, alias="_needsDisambiguation")
    actor: list[_Relation] = Field(default_factory=list)
    histrion: list[_Relation] = Field(default_factory=list)
    director: list[_Relation] = Field(default_factory=list)
    maker: list[_Relation] = Field(default_factory=list)
    label: list[_Relation] = Field(default_factory=list)
    series: list[_Relation] = Field(default_factory=list)
    genre: list[_Relation] = Field(default_factory=list)


class _SampleImages(ResponseModel):
    image: list[str] = Field(default_factory=list)


class _DmmSamples(ResponseModel):
    sample_l: _SampleImages | None = None
    sample_s: _SampleImages | None = None


class _DmmData(ResponseModel):
    content_id: str = ""
    samples: _DmmSamples | None = Field(default=None, alias="sampleImageURL")


class _Page(ResponseModel):
    movie: _Movie | None = None
    movies: list[_MovieRef] = Field(default_factory=list)
    dmm: _DmmData | None = Field(default=None, alias="dmmData")


def _number(value: str) -> str | None:
    number = value.strip().upper()
    if not _NUMBER.fullmatch(number) or fc2_article_id(number) is not None:
        return None
    match = _UNDASHED.fullmatch(number)
    return f"{match[1]}-{match[2]}" if match else number


def _matches(movie: _MovieRef, requested: str) -> bool:
    # 别名含厂牌数字段时以完整别名比较, 防止 200MIUM 命中 300MIUM.
    return number_match(movie.canonical_number, requested) is not NumberMatch.NONE


class AvWikiDBCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.AVWIKIDB, base_url="https://avwikidb.com", urls=["https://avwikidb.com"])

    def serves_url(self, url: str) -> bool:
        return self._url_number(url) is not None

    def _work_url(self, number: str) -> str:
        return f"{self.base_url}/work/{quote(number, safe='')}/"

    def _headers(self) -> dict[str, str]:
        return {"Referer": f"{self.base_url}/", **self.headers}

    async def check_connectivity(self) -> ConnectivityOutcome:
        return await self.client.check(self.base_url, cookies=self.cookies, headers=self._headers())

    def _url_number(self, url: str) -> str | None:
        try:
            parsed = urlsplit(url)
            candidates = (urlsplit(self.base_url), urlsplit(self._profile.base_url))
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        for entry in candidates:
            if parsed.netloc.lower().removeprefix("www.") != entry.netloc.lower().removeprefix("www."):
                continue
            prefix = f"{entry.path.rstrip('/')}/work/"
            if parsed.path.startswith(prefix):
                return _number(unquote(parsed.path[len(prefix) :].removesuffix("/")))
        return None

    async def _page(self, url: str) -> _Page | None:
        try:
            text = await self.client.get_html(url, cookies=self.cookies, headers=self._headers())
        except SourceError as exc:
            if exc.reason is FailureReason.NOT_FOUND:
                return None
            raise
        props = next_data_props(text)
        if props is None or not ({"movie", "movies"} & props.keys()):
            raise SourceError(FailureReason.PARSE_ERROR, detail="missing AVWikiDB page data", url=url)
        try:
            return _Page.model_validate(props)
        except ValidationError as exc:
            raise SourceError(FailureReason.PARSE_ERROR, detail=parse_detail(exc), url=url) from exc

    def _result(self, page: _Page, requested: str) -> MediaMetadata | None:
        movie = page.movie
        if movie is None or movie.ambiguous or not _matches(movie, requested):
            return None
        canonical = _number(movie.canonical_number)
        if canonical is None:
            return None
        return _metadata(movie, page.dmm, self._work_url(canonical), self.base_url)

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        number = _number(query.number)
        if number is None:
            return None
        page = await self._page(self._work_url(number))
        if page is not None and (result := self._result(page, number)) is not None:
            return result
        for term in dict.fromkeys((number, number.replace("-", ""))):
            page = await self._page(f"{self.base_url}/search/?q={quote(term, safe='')}")
            if page is None:
                continue
            if page.movie is not None:
                if result := self._result(page, number):
                    return result
                continue
            index = select_number_index([movie.canonical_number for movie in page.movies], number)
            if index is None:
                continue
            picked = _number(page.movies[index].canonical_number)
            if picked is not None:
                detail = await self._page(self._work_url(picked))
                return self._result(detail, number) if detail is not None else None
        return None

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        result = await self.fetch(query, options)
        return result.source_url if result is not None else None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        number = self._url_number(url)
        if number is None:
            return None
        page = await self._page(self._work_url(number))
        return self._result(page, number) if page is not None else None


def _http_url(value: str | None, base_url: str) -> str | None:
    if not value:
        return None
    url = value.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/"):
        url = f"{base_url}{url}"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    return (
        url
        if parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password)
        else None
    )


def _mgs_images(value: str | None, base_url: str) -> list[str]:
    proxy = _http_url(value, base_url)
    if proxy is None:
        return []
    if value and value.startswith("/mgsimg/images/") and ".." not in value.split("/"):
        return [f"https://image.mgstage.com/{value.removeprefix('/mgsimg/')}", proxy]
    return [proxy]


def _images(movie: _Movie, dmm: _DmmData | None, base_url: str) -> tuple[list[str], list[str], list[str]]:
    thumbs = _mgs_images(movie.mgs_image, base_url)
    posters = [url.replace("/pb_e_", "/pf_e_") for url in thumbs if "/pb_e_" in url]
    samples = [urls[0] for value in movie.mgs_samples if (urls := _mgs_images(value, base_url))]
    cid = movie.cid
    if not cid or not _CID.fullmatch(cid) or movie.floor not in {"videoa", "videovr", "videoc"}:
        return posters, thumbs, list(dict.fromkeys(samples))
    amateur = movie.floor == "videoc"
    cabinet = "amateur" if amateur else "video"
    stem = f"digital/{cabinet}/{cid}/{cid}"
    thumbs.extend(image_candidates(f"{PICS_CDN}/{stem}{'jp' if amateur else 'pl'}.jpg"))
    posters.extend(image_candidates(f"{PICS_CDN}/{stem}{'jp' if amateur else 'ps'}.jpg"))
    dmm_samples = dmm.samples if dmm is not None and dmm.content_id == cid else None
    provided = dmm_samples.sample_l if dmm_samples is not None else None
    if (provided is None or not provided.image) and dmm_samples is not None:
        provided = dmm_samples.sample_s
    dmm_images: list[str] = []
    for value in provided.image if provided is not None else []:
        url = _http_url(value, base_url)
        if url is None:
            continue
        clean = url.partition("?")[0].partition("#")[0]
        if any(
            clean.startswith(prefix := f"{root}/{stem}")
            and re.fullmatch(r"(?:jp|js)?-[0-9]+\.jpg", clean[len(prefix) :])
            for root in (PICS_CDN, HIGH_RES_CDN)
        ):
            dmm_images.append(image_candidates(url)[0])
    if not dmm_images and movie.image_count is not None and 0 < movie.image_count <= 100:
        for index in range(1, movie.image_count + 1):
            suffix = (
                f"{'jp' if movie.large_images else 'js'}-{index:03d}"
                if amateur
                else f"{'jp' if movie.large_images else ''}-{index}"
            )
            dmm_images.append(f"{HIGH_RES_CDN}/{stem}{suffix}.jpg")
    return list(dict.fromkeys(posters)), list(dict.fromkeys(thumbs)), list(dict.fromkeys([*samples, *dmm_images]))


def _metadata(movie: _Movie, dmm: _DmmData | None, url: str, base_url: str) -> MediaMetadata:
    posters, thumbs, samples = _images(movie, dmm, base_url)
    actors = film_actors(dict.fromkeys(item.actor.name for item in movie.actor if item.actor and item.actor.name))
    actors.extend(
        film_actors(
            dict.fromkeys(item.histrion.name for item in movie.histrion if item.histrion and item.histrion.name),
            gender=ActorGender.MALE,
        )
    )
    trailer = _http_url(movie.trailer, base_url)
    return MediaMetadata(
        number=movie.canonical_number,
        title=movie.title,
        actors=actors,
        studio=next((item.maker.name for item in movie.maker if item.maker and item.maker.name), None),
        publisher=next((item.label.name for item in movie.label if item.label and item.label.name), None),
        series=next((item.series.name for item in movie.series if item.series and item.series.name), None),
        release=movie.release,
        runtime=movie.runtime if movie.runtime is not None and movie.runtime > 0 else None,
        directors=list(
            dict.fromkeys(item.director.name for item in movie.director if item.director and item.director.name)
        ),
        tags=list(dict.fromkeys(item.genre.name for item in movie.genre if item.genre and item.genre.name)),
        plot=movie.summary,
        poster_urls=posters,
        thumb_urls=thumbs,
        extrafanart=samples,
        trailer_urls=[trailer] if trailer else [],
        external_id=str(movie.id) if movie.id is not None else None,
        source_url=url,
    )
