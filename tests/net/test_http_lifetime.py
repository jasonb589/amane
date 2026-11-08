"""热更新时下载正文、分片与取消操作的客户端释放顺序."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from curl_cffi.requests import Response

from amane.net.http import RateLimiters, WebClient
from tests.helpers import LOAD_TOLERANT_TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("bounded,cancel", [(False, False), (True, False), (True, True)])
async def test_retired_download_finishes_body_before_session_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bounded: bool, cancel: bool
) -> None:
    web = WebClient(limiters=RateLimiters(default_rate=10000), chunked_threshold=4, chunk_size=4)
    waiting, proceed, closed, body_closed = (asyncio.Event() for _ in range(4))
    calls: list[str] = []
    close_count = 0
    original_close = web._session.close

    class Stream(Response):
        def __init__(self) -> None:
            super().__init__()
            self.status_code = 200
            self.quit_now = asyncio.Event()

        async def aiter_content(
            self, chunk_size: int | None = None, decode_unicode: bool = False
        ) -> AsyncIterator[bytes]:
            yield b"abcd"
            waiting.set()
            await proceed.wait()
            assert not closed.is_set()
            yield b"efgh"

        async def aclose(self) -> None:
            body_closed.set()

    async def request(method: str, url: str, **kwargs: Any) -> Response:
        assert not closed.is_set()
        calls.append(method)
        if bounded:
            return Stream()
        response = Response()
        response.status_code = 200
        if method == "HEAD":
            waiting.set()
            await proceed.wait()
            response.headers["Content-Length"] = "8"
        else:
            start, end = map(int, kwargs["headers"]["Range"].removeprefix("bytes=").split("-"))
            response.status_code = 206
            response.content = b"abcdefgh"[start : end + 1]
        return response

    async def close() -> None:
        nonlocal close_count
        close_count += 1
        if bounded:
            assert body_closed.is_set()
        await original_close()
        closed.set()

    monkeypatch.setattr(web._session, "request", request)
    monkeypatch.setattr(web._session, "close", close)
    path = tmp_path / "image.bin"
    task = asyncio.create_task(web.download("https://images.test/image", path, max_bytes=32 if bounded else None))
    try:
        await asyncio.wait_for(waiting.wait(), LOAD_TOLERANT_TIMEOUT)
        web.retire()
        assert not closed.is_set()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            proceed.set()
            assert await asyncio.wait_for(task, LOAD_TOLERANT_TIMEOUT)
            assert path.read_bytes() == b"abcdefgh"
            assert calls == (["GET"] if bounded else ["HEAD", "GET", "GET"])
        await asyncio.wait_for(closed.wait(), LOAD_TOLERANT_TIMEOUT)
        await web.close()
        assert close_count == 1
        with pytest.raises(RuntimeError, match="资源已开始关闭"):
            await web.request("GET", "https://images.test/late")
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await web.close()


@pytest.mark.asyncio
async def test_cancelling_close_waiter_keeps_single_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    web = WebClient(limiters=RateLimiters())
    closing, proceed = asyncio.Event(), asyncio.Event()
    original_close = web._session.close
    close_count = 0

    async def close() -> None:
        nonlocal close_count
        close_count += 1
        closing.set()
        await proceed.wait()
        await original_close()

    monkeypatch.setattr(web._session, "close", close)
    with web.lease():
        web.retire()
        with web.lease():
            assert not closing.is_set()
    waiter = asyncio.create_task(web.close())
    await asyncio.wait_for(closing.wait(), LOAD_TOLERANT_TIMEOUT)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    proceed.set()
    await asyncio.wait_for(web.close(), LOAD_TOLERANT_TIMEOUT)
    assert close_count == 1
