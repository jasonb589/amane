"""一本道完整 API 回放、目录隔离、公开图片与配置会话."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest
from curl_cffi.requests import Response

from amane.aggregate import aggregate, compile_priority
from amane.config import SiteConfig
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.onepondo import OnePondoCrawler
from amane.enums import BrowserMode
from amane.net.connectivity import ConnectivityStatus
from amane.net.errors import FailureKind, FailureReason, RequestError, RequestFailure, SourceError
from amane.parsing import ContentType

from .driven import assert_expected, build_mock, call_step, load_response_file, load_toml

pytestmark = pytest.mark.asyncio
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "1pondo"


def _movie(number: str = "111226_001") -> dict[str, object]:
    data: dict[str, object] = json.loads((_FIXTURES / f"{number}.json").read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("case", sorted(_FIXTURES.glob("*.toml")), ids=lambda path: path.stem)
async def test_captured_api_responses(case: Path, http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    config = load_toml(case)["fetch"]
    build_mock(mock_web_client, case.parent, config["responses"])
    for response in config["responses"]:
        if response.get("status") == 404:
            body = load_response_file(case.parent, response["file"])
            assert isinstance(body, str)
            mock_web_client.get_json.side_effect = RequestError(
                response["url_contains"],
                RequestFailure(kind=FailureKind.HTTP_STATUS, message="HTTP 404", status=404, body=body.encode("utf-8")),
            )
    result = await call_step(OnePondoCrawler(http_client).fetch(SearchQuery(config["number"])), config)
    if config.get("expected_none"):
        assert result is None
    elif not config.get("expected_error"):
        assert result is not None
        assert_expected(result, config["expected"])
    assert mock_web_client.get_json.await_count == config["requests"]
    mock_web_client.get_text.assert_not_awaited()


@pytest.mark.parametrize(
    "number",
    [
        "",
        "011516-227",
        "011516_27",
        "011516_0227",
        "011516_000",
        "022926_001",
        "133226_001",
        "FC2-1234567",
        "HEYZO-1234",
        "caribbeancom_011516_227",
        "10musume_011516_227",
        "../011516_227",
        "011516_227?x=1",
        "011516_227_extra",
        "1PONDO-011516-227-other",
    ],
)
async def test_unrelated_or_invalid_number_does_not_request(
    number: str, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    assert await OnePondoCrawler(http_client).fetch(SearchQuery(number)) is None
    mock_web_client.get_json.assert_not_awaited()


@pytest.mark.parametrize("kind", [kind for kind in ContentType if kind is not ContentType.UNCENSORED])
async def test_other_content_types_do_not_request(
    kind: ContentType, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    assert await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001", content_type=kind)) is None
    mock_web_client.get_json.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.1pondo.tv.evil.example/movies/111226_001/",
        "https://www.1pondo.tv@evil.example/movies/111226_001/",
        "https://user:secret@www.1pondo.tv/movies/111226_001/",
        "https://www.1pondo.tv/movies/111226-001/",
        "https://www.1pondo.tv/movies/111226_01/",
        "https://www.1pondo.tv/movies/111226_001/other",
        "https://www.1pondo.tv/movies/%31%31%31%32%32%36_001/",
        "https://www.1pondo.tv/movies/../111226_001/",
        "https://www.1pondo.tv/actresses/111226_001/",
        "https://[invalid/movies/111226_001/",
        "file:///movies/111226_001/",
    ],
)
async def test_unrelated_urls_do_not_request(url: str, http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    crawler = OnePondoCrawler(http_client)
    assert not crawler.serves_url(url)
    assert await crawler.fetch_from_url(url) is None
    mock_web_client.get_json.assert_not_awaited()


@pytest.mark.parametrize("number", ["111026_001", "111226-001", "111226_01", "011516_227"])
async def test_response_identity_is_exact_before_fetching_gallery(
    number: str, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_json.return_value = {**_movie(), "MovieID": number}
    assert await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001")) is None
    assert mock_web_client.get_json.await_count == 1


@pytest.mark.parametrize(
    "payload", [None, [], "<html>layout changed</html>", {}, {"MovieID": "111226_001"}, {"MovieID": 123}]
)
async def test_invalid_movie_schema_reports_parse_error(
    payload: object, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_json.return_value = payload
    with pytest.raises(SourceError) as exc:
        await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001"))
    assert exc.value.reason is FailureReason.PARSE_ERROR
    assert exc.value.url == "https://www.1pondo.tv/dyn/phpauto/movie_details/movie_id/111226_001.json"
    assert mock_web_client.get_json.await_count == 1


@pytest.mark.parametrize(
    "reason", [FailureReason.SERVER_ERROR, FailureReason.RATE_LIMITED, FailureReason.CLOUDFLARE_CHALLENGE]
)
async def test_movie_request_failure_propagates(
    reason: FailureReason, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_json.side_effect = SourceError(reason)
    with pytest.raises(SourceError) as exc:
        await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001"))
    assert exc.value.reason is reason
    assert mock_web_client.get_json.await_count == 1


@pytest.mark.parametrize("failure", [SourceError(FailureReason.SERVER_ERROR), SourceError(FailureReason.NOT_FOUND), []])
async def test_gallery_failure_keeps_confirmed_movie(
    failure: object, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_json.side_effect = [_movie(), failure]
    result = await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001"))
    assert result is not None and result.title == _movie()["Title"]
    assert result.extrafanart == []
    assert mock_web_client.get_json.await_count == 2


@pytest.mark.parametrize("legacy", [False, True])
async def test_gallery_identity_and_protection_do_not_leak_foreign_or_member_images(
    legacy: bool, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    movie = {**_movie(), "Gallery": not legacy, "HasGallery": legacy}
    valid_path = "movie_gallery/sample/111226_001/public.jpg"
    row = {"MovieID": "111226_001", "Filename": "1.jpg", "Img": valid_path, "Protected": False}
    gallery = {
        "MovieID": "111226_001",
        "MetaMovieID": movie["MetaMovieID"],
        "Rows": [
            row,
            row,
            {**row, "Protected": True},
            {**row, "Protected": None},
            {**row, "MovieID": "111026_001", "Img": valid_path.replace("111226_001", "111026_001")},
            {**row, "Filename": "../1.jpg", "Img": "movie_gallery/sample/111226_001/../other.jpg"},
            {**row, "Img": "movie_gallery/member/111226_001/member.jpg", "Protected": True},
        ],
    }
    mock_web_client.get_json.side_effect = [movie, gallery]
    result = await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001"))
    expected = (
        "https://www.1pondo.tv/assets/sample/111226_001/popu/1.jpg"
        if legacy
        else f"https://www.1pondo.tv/dyn/dla/images/{valid_path}"
    )
    assert result is not None and result.extrafanart == [expected]
    mock_web_client.get_json.side_effect = [movie, {**gallery, "MovieID": "111026_001", "MetaMovieID": -1}]
    result = await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001"))
    assert result is not None and result.extrafanart == []


async def test_nullable_metadata_and_resource_identity(http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    movie = {
        **_movie(),
        "ActressesJa": None,
        "Actor": "Actor A, Actor B",
        "UCNAME": None,
        "Desc": None,
        "Series": None,
        "Duration": -1,
        "AvgRating": 0,
        "Gallery": None,
        "HasGallery": None,
        "ThumbUltra": "https://www.1pondo.tv/moviepages/111026_001/images/str.jpg",
        "ThumbHigh": "https://evil.example/moviepages/111226_001/images/str.jpg",
        "ThumbMed": None,
        "ThumbLow": "file:///image.jpg",
        "MovieThumb": None,
        "SampleFiles": [
            {"FileName": "1080p.mp4", "URL": "https://smovie.1pondo.tv/sample/movies/111226_001/1080p.mp4"},
            {"FileName": "2160p.mp4", "URL": "https://smovie.1pondo.tv/member/movies/111226_001/2160p.mp4"},
            {"FileName": "720p.mp4", "URL": "https://1pondo.tv.evil.example/sample/movies/111226_001/720p.mp4"},
            {"FileName": "480p.mp4", "URL": "https://smovie.1pondo.tv/sample/movies/111026_001/480p.mp4"},
        ],
    }
    mock_web_client.get_json.return_value = movie
    result = await OnePondoCrawler(http_client).fetch(SearchQuery("111226_001"))
    assert result is not None
    assert [(actor.name, actor.gender.value) for actor in result.actors] == [
        ("Actor A", "unknown"),
        ("Actor B", "unknown"),
    ]
    assert result.plot is result.runtime is result.series is None and result.score == 0
    assert result.tags == result.extrafanart == []
    assert result.thumb_urls == result.poster_urls == ["https://www.1pondo.tv/assets/sample/111226_001/str.jpg"]
    assert result.trailer_urls == ["https://smovie.1pondo.tv/sample/movies/111226_001/1080p.mp4"]
    assert mock_web_client.get_json.await_count == 1


@pytest.mark.parametrize(
    "url",
    ["https://1pondo.tv/movies/111226_001/", "https://www.1pondo.tv/moviepages/111226_001/index.html?from=bookmark"],
)
async def test_configured_mirror_uses_prefix_and_same_session_for_details_gallery_and_probe(
    url: str, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    config = SiteConfig(
        base_url="https://mirror.example/onepondo/", cookie={"session": "user-cookie"}, use_browser=BrowserMode.ALWAYS
    )
    crawler = OnePondoCrawler(http_client.for_source("1pondo", config), config)
    mock_web_client.get_json.side_effect = [
        _movie(),
        json.loads((_FIXTURES / "gallery-111226_001.json").read_text(encoding="utf-8")),
    ]
    result = await crawler.fetch_from_url(url)
    assert result is not None and result.source_url == "https://mirror.example/onepondo/movies/111226_001/"
    assert all(
        image.startswith("https://mirror.example/onepondo/") for image in [*result.thumb_urls, *result.extrafanart]
    )
    assert mock_web_client.get_json.await_args_list == [
        call(
            "https://mirror.example/onepondo/dyn/phpauto/movie_details/movie_id/111226_001.json",
            cookies={"session": "user-cookie"},
            headers={"Referer": "https://mirror.example/onepondo/"},
        ),
        call(
            "https://mirror.example/onepondo/dyn/dla/json/movie_gallery/111226_001.json",
            cookies={"session": "user-cookie"},
            headers={"Referer": "https://mirror.example/onepondo/"},
        ),
    ]
    response = Response()
    response.url = "https://mirror.example/onepondo/dyn/phpauto/movie_lists/list_newest_0.json"
    response.status_code = 200
    response.content = b'{"TotalRows":1,"Rows":[]}'
    mock_web_client.request.return_value = response
    assert (await crawler.check_connectivity()).status is ConnectivityStatus.OK
    mock_web_client.request.assert_awaited_once_with(
        "GET",
        response.url,
        cookies={"session": "user-cookie"},
        headers={"Referer": "https://mirror.example/onepondo/"},
        max_attempts=1,
    )
    mock_web_client.get_text.assert_not_awaited()


async def test_native_factory_and_parallel_queries_keep_movie_data_isolated(
    http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    factory = CrawlerFactory(http_client, {"1pondo": SiteConfig()})
    crawler = await factory.get("1pondo")
    assert isinstance(crawler, OnePondoCrawler) and await factory.get("1pondo") is crawler

    async def get_json(url: str, **kwargs: object) -> object:
        await asyncio.sleep(0)
        name = url.rsplit("/", 1)[-1]
        if "movie_galleries" in url:
            name = f"gallery-legacy-{name}"
        elif "movie_gallery" in url:
            name = f"gallery-{name}"
        return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))

    mock_web_client.get_json.side_effect = get_json
    old, new = await asyncio.gather(crawler.fetch(SearchQuery("011516_227")), crawler.fetch(SearchQuery("111226_001")))
    assert old is not None and new is not None
    assert (old.number, new.number) == ("011516_227", "111226_001")
    assert all("011516_227" in url for url in old.extrafanart)
    assert all("111226_001" in url for url in new.extrafanart)
    mock_web_client.get_json.reset_mock()
    result = await aggregate(
        SearchQuery("111226_001", url="https://www.1pondo.tv/movies/111226_001/"),
        {"1pondo": crawler},
        compile_priority(["1pondo"], {}),
    )
    assert result.metadata.title == new.title
    assert result.metadata.source_urls == {"1pondo": new.source_url}
    assert mock_web_client.get_json.await_count == 2
