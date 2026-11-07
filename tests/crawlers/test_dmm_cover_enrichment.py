"""DMM 跨柜台封面补充: 复用搜索、同番号约束与失败隔离."""

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.dmm import DmmCrawler
from amane.net.errors import FailureReason, SourceError

if TYPE_CHECKING:
    from amane.crawlers.http import HttpClient


@pytest.mark.asyncio
@pytest.mark.parametrize("supplement_failure", [None, FailureReason.TIMEOUT, FailureReason.AGE_VERIFICATION])
async def test_fetch_reuses_search_and_bounds_retail_cover_supplement(
    supplement_failure: FailureReason | None,
) -> None:
    digital = "https://video.dmm.co.jp/av/content/?id=abf00295"
    retail = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295/"
    other_retail = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=100abf295/"
    search = json.dumps([{"detailUrl": url} for url in [retail, digital, other_retail]])
    retail_html = (
        '<meta property="og:image" content="https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg">'
        '<script type="application/ld+json">{"name":"通販別表記",'
        '"image":"https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg"}</script>'
    )
    calls: list[str] = []

    async def get_html(url: str, **_kwargs: object) -> str:
        calls.append(url)
        if "/search/" in url:
            return f"<script>{search}</script>"
        assert url == retail
        if supplement_failure:
            raise SourceError(supplement_failure, detail="supplement unavailable")
        return retail_html

    client = SimpleNamespace(
        get_html=get_html,
        post_json=AsyncMock(
            return_value={
                "data": {
                    "ppvContent": {
                        "title": "配信原題",
                        "description": "配信紹介文",
                        "packageImage": {
                            "largeUrl": "https://pics.dmm.co.jp/digital/video/abf00295/abf00295pl.jpg",
                            "mediumUrl": "https://pics.dmm.co.jp/digital/video/abf00295/abf00295ps.jpg",
                        },
                    }
                }
            }
        ),
    )
    result = await DmmCrawler(cast("HttpClient", client)).fetch(SearchQuery("ABF-295"))
    assert result is not None
    assert result.title == "配信原題"
    assert result.plot == "配信紹介文"
    assert result.source_url == digital
    assert result.external_id == digital
    assert calls == ["https://www.dmm.co.jp/search/=/searchstr=abf00295/sort=ranking/", retail]
    assert client.post_json.await_count == 1
    assert result.thumb_urls[:2] == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/abf00295/abf00295pl.jpg",
        "https://pics.dmm.co.jp/digital/video/abf00295/abf00295pl.jpg",
    ]
    assert result.poster_urls[:2] == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/abf00295/abf00295ps.jpg",
        "https://pics.dmm.co.jp/digital/video/abf00295/abf00295ps.jpg",
    ]
    if supplement_failure:
        assert len(result.thumb_urls) == len(result.poster_urls) == 2
    else:
        assert result.thumb_urls[2:] == [
            "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg",
            "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg",
        ]
        assert result.poster_urls[2:] == [
            "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295ps.jpg",
            "https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295ps.jpg",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cover", "accepted"),
    [
        ("https://pics.dmm.co.jp/mono/movie/adult/118abf296/118abf296pl.jpg", False),
        ("https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf296pl.jpg", False),
        ("https://pics.dmm.co.jp/mono/movie/adult/118abf296/118abf295pl.jpg", False),
        ("https://pics.dmm.co.jp/mono/movie/adult/118abf295tk1/118abf295tk1pl.jpg", False),
        ("https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295jp-1.jpg", False),
        ("https://pics.dmm.co.jp/p/ms/top/logo.jpg", False),
        ("https://pics.dmm.co.jp.evil.example/mono/movie/adult/118abf295/118abf295pl.jpg", False),
        ("https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg?w=384", True),
        ("https://pics.dmm.co.jp/mono/movie/adult/118ABF295/118ABF295ps.jpg?w=147", True),
    ],
)
async def test_supplement_images_must_belong_to_selected_retail_cid(cover: str, accepted: bool) -> None:
    digital = "https://video.dmm.co.jp/av/content/?id=abf00295"
    retail = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295/"
    search = json.dumps([{"detailUrl": url} for url in [digital, retail]])
    primary = "https://pics.dmm.co.jp/digital/video/abf00295/abf00295pl.jpg"
    client = SimpleNamespace(
        get_html=AsyncMock(side_effect=[f"<script>{search}</script>", f'<meta property="og:image" content="{cover}">']),
        post_json=AsyncMock(
            return_value={"data": {"ppvContent": {"title": "配信原題", "packageImage": {"largeUrl": primary}}}}
        ),
    )
    result = await DmmCrawler(cast("HttpClient", client)).fetch(SearchQuery("ABF-295"))
    assert result is not None
    assert result.source_url == digital
    assert result.thumb_urls[:2] == [
        "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/abf00295/abf00295pl.jpg",
        primary,
    ]
    if accepted:
        assert cover in result.thumb_urls
        assert len(result.poster_urls) > 2
    else:
        assert len(result.thumb_urls) == len(result.poster_urls) == 2
    assert client.get_html.await_count == 2
    assert client.post_json.await_count == 1


@pytest.mark.parametrize(
    ("wanted", "found", "candidate", "expected"),
    [
        ("ABF-295", "ABF-295", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295/", True),
        ("ABF-007", "ABF-7", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf007/", True),
        ("GODR-1287", "GODR-1287", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=h_1059godr1287/", True),
        ("ABF-295", "ABF-296", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295/", False),
        ("ABF-295", "ABF-295", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295a/", False),
        ("ABF-295", "ABF-295", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295r/", False),
        ("ABF-295", "ABF-295", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295tk1/", False),
        ("ABF-295", "ABF-295", "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf296/", False),
        ("ABF-295", "ABF-295", "https://www.dmm.co.jp/rental/-/detail/=/cid=118abf295/", False),
        ("ABF-295", "ABF-295", "https://evil.example/mono/dvd/-/detail/=/cid=118abf295/", False),
        ("ABF-295", "ABF-295", "file://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295/", False),
        ("ABF-295", "ABF-295", "https://[invalid/mono/dvd/-/detail/=/cid=118abf295/", False),
    ],
)
def test_retail_supplement_requires_same_catalog_number(
    wanted: str,
    found: str,
    candidate: str,
    expected: bool,
) -> None:
    crawler = DmmCrawler(cast("HttpClient", SimpleNamespace()))
    assert crawler._retail_cover_url(wanted, found, [candidate]) == (candidate if expected else None)


@pytest.mark.asyncio
async def test_explicit_url_does_not_search_or_add_retail_requests() -> None:
    url = "https://video.dmm.co.jp/av/content/?id=abf00295"
    client = SimpleNamespace(
        get_html=AsyncMock(side_effect=AssertionError("unexpected search")),
        post_json=AsyncMock(return_value={"data": {"ppvContent": {"title": "配信原題"}}}),
    )
    crawler = DmmCrawler(cast("HttpClient", client))
    result = await crawler.fetch(SearchQuery("ABF-295", url=url))
    assert result is not None
    assert result.source_url == url
    assert client.get_html.await_count == 0
    assert client.post_json.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_retail", "primary_missing", "retail_primary"])
async def test_fetch_skips_unneeded_supplement_requests(case: str) -> None:
    digital = "https://video.dmm.co.jp/av/content/?id=abf00295"
    retail = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=118abf295/"
    candidates = [digital] if case == "no_retail" else [retail] if case == "retail_primary" else [digital, retail]
    responses = ["<script>" + json.dumps([{"detailUrl": url} for url in candidates]) + "</script>"]
    if case == "retail_primary":
        responses.append(
            '<h1>通販原題</h1><meta property="og:image" '
            'content="https://pics.dmm.co.jp/mono/movie/adult/118abf295/118abf295pl.jpg">'
        )
    client = SimpleNamespace(
        get_html=AsyncMock(side_effect=responses),
        post_json=AsyncMock(
            return_value={"data": {"ppvContent": None if case == "primary_missing" else {"title": "配信原題"}}}
        ),
    )
    result = await DmmCrawler(cast("HttpClient", client)).fetch(SearchQuery("ABF-295"))
    if case == "primary_missing":
        assert result is None
    else:
        assert result is not None
        assert result.source_url == (retail if case == "retail_primary" else digital)
    assert client.get_html.await_count == (2 if case == "retail_primary" else 1)
    assert client.post_json.await_count == (0 if case == "retail_primary" else 1)


@pytest.mark.asyncio
async def test_concurrent_fetches_keep_retail_candidates_local() -> None:
    pending = 0
    both_started = asyncio.Event()
    calls: list[str] = []

    async def get_html(url: str, **_kwargs: object) -> str:
        nonlocal pending
        calls.append(url)
        if "/search/" in url:
            cid = "abf00295" if "abf00295" in url else "abf00296"
            retail_cid = "118abf295" if cid == "abf00295" else "118abf296"
            pending += 1
            if pending == 2:
                both_started.set()
            await both_started.wait()
            candidates = [
                f"https://video.dmm.co.jp/av/content/?id={cid}",
                f"https://www.dmm.co.jp/mono/dvd/-/detail/=/cid={retail_cid}/",
            ]
            return "<script>" + json.dumps([{"detailUrl": candidate} for candidate in candidates]) + "</script>"
        cid = "118abf295" if "118abf295" in url else "118abf296"
        return f'<meta property="og:image" content="https://pics.dmm.co.jp/mono/movie/adult/{cid}/{cid}pl.jpg">'

    client = SimpleNamespace(
        get_html=get_html,
        post_json=AsyncMock(return_value={"data": {"ppvContent": {"title": "配信原題"}}}),
    )
    crawler = DmmCrawler(cast("HttpClient", client))
    results = await asyncio.gather(crawler.fetch(SearchQuery("ABF-295")), crawler.fetch(SearchQuery("ABF-296")))
    for number, result in zip((295, 296), results, strict=True):
        assert result is not None
        assert result.thumb_urls == [
            f"https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf{number}/118abf{number}pl.jpg",
            f"https://pics.dmm.co.jp/mono/movie/adult/118abf{number}/118abf{number}pl.jpg",
        ]
    assert len(calls) == 4
