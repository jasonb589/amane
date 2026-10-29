"""r18 dump 自动取最新: {latest} 占位符的列举与解析."""

from __future__ import annotations

from typing import Any

import pytest

from amane.config import R18Config
from amane.crawlers.r18dev.importer import R18Importer, _split_latest, parse_s3_listing

ROOT = "https://bucket.example"


def _listing(keys: list[tuple[str, str]], *, token: str | None = None, truncated: bool = False) -> str:
    contents = "".join(
        f"<Contents><Key>{key}</Key><LastModified>{modified}</LastModified>"
        f"<ETag>&quot;x&quot;</ETag><Size>1</Size></Contents>"
        for key, modified in keys
    )
    tail = ""
    if truncated:
        tail = "<IsTruncated>true</IsTruncated>"
        if token:
            tail += f"<NextContinuationToken>{token}</NextContinuationToken>"
    else:
        tail = "<IsTruncated>false</IsTruncated>"
    return f"<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>{contents}{tail}</ListBucketResult>"


class _Resp:
    def __init__(self, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.text = text
        self.headers = headers or {}


class _FakeWeb:
    """按顺序应答: 元素为 str 时当正文, 为 dict 时当 HEAD 响应头."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> _Resp:
        del kwargs
        self.calls.append((method, url))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return _Resp(headers=item)
        return _Resp(text=item)


def _importer(url: str, responses: list[Any]) -> tuple[R18Importer, _FakeWeb]:
    web = _FakeWeb(responses)
    return R18Importer(R18Config(download_url=url), web), web  # type: ignore[arg-type]


def test_split_latest_splits_host_and_prefix() -> None:
    assert _split_latest(f"{ROOT}/dumps/{{latest}}") == (ROOT, "dumps/")


def test_split_latest_returns_none_without_placeholder() -> None:
    assert _split_latest(f"{ROOT}/dumps/dump_2026-10-27.sql.gz") is None


def test_parse_s3_listing_strips_namespace() -> None:
    entries, token = parse_s3_listing(_listing([("dumps/a.sql.gz", "2026-10-01T03:00:00.000Z")]))
    assert entries == [("dumps/a.sql.gz", "2026-10-01T03:00:00.000Z")]
    assert token is None


@pytest.mark.asyncio
async def test_resolve_latest_picks_newest_sql_gz() -> None:
    """按 LastModified 取最新, 且跳过非 .sql.gz 的对象."""
    listing = _listing(
        [
            ("dumps/r18_2026-10-13.sql.gz", "2026-10-13T03:03:22.000Z"),
            ("dumps/r18_2026-10-27.sql.gz", "2026-10-27T03:03:32.000Z"),
            ("dumps/", "2026-10-29T00:00:00.000Z"),
        ]
    )
    importer, web = _importer(f"{ROOT}/dumps/{{latest}}", [listing])

    assert await importer._resolve_download_url() == f"{ROOT}/dumps/r18_2026-10-27.sql.gz"
    assert web.calls == [("GET", f"{ROOT}/?list-type=2&max-keys=1000&prefix=dumps/")]


@pytest.mark.asyncio
async def test_resolve_latest_leaves_plain_url_untouched() -> None:
    """没有占位符时不发任何请求, 保持原地址 (向后兼容)."""
    url = f"{ROOT}/dumps/r18_2026-10-13.sql.gz"
    importer, web = _importer(url, [])

    assert await importer._resolve_download_url() == url
    assert web.calls == []


@pytest.mark.asyncio
async def test_resolve_latest_follows_continuation_token() -> None:
    """第一页被截断时继续翻页, 后面的更新版本要能赢."""
    page1 = _listing([("dumps/r18_2026-10-06.sql.gz", "2026-10-06T03:00:00.000Z")], token="tok", truncated=True)
    page2 = _listing([("dumps/r18_2026-10-27.sql.gz", "2026-10-27T03:00:00.000Z")])
    importer, web = _importer(f"{ROOT}/dumps/{{latest}}", [page1, page2])

    assert await importer._resolve_download_url() == f"{ROOT}/dumps/r18_2026-10-27.sql.gz"
    assert web.calls[1] == ("GET", f"{ROOT}/?list-type=2&max-keys=1000&prefix=dumps/&continuation-token=tok")


@pytest.mark.asyncio
async def test_fetch_remote_meta_heads_resolved_dump() -> None:
    """HEAD 打的是解析出来的新文件, 而不是配置里那份旧文件."""
    listing = _listing([("dumps/r18_2026-10-27.sql.gz", "2026-10-27T03:03:32.000Z")])
    head = {"content-length": "262800000", "etag": '"abc-33"'}
    importer, web = _importer(f"{ROOT}/dumps/{{latest}}", [listing, head, head])

    meta = await importer.fetch_remote_meta()

    assert meta is not None
    assert meta.filename == "r18_2026-10-27.sql.gz"
    assert meta.size == 262800000
    assert meta.etag == "abc-33"
    assert web.calls[-1] == ("HEAD", f"{ROOT}/dumps/r18_2026-10-27.sql.gz")
