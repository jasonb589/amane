"""API 网络快照跨配置重建保持有效, 请求取消与流式响应完成后释放."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from curl_cffi import Response as CurlResponse

from amane.app.runtime import AppRuntime
from amane.net.http import WebClient
from amane.playback.factory import PlaybackFactory
from amane.plugins.api import PlaybackOffer, PlaybackQuery, PlaybackTarget
from tests.helpers import LOAD_TOLERANT_TIMEOUT
from tests.plugins.test_plugin_system import playback_plugin_source, write_plugin

if TYPE_CHECKING:
    from fastapi import FastAPI

    from amane.db.repository import Repository

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
_SOURCE = "acme.lifetime"


class _ImageResponse(CurlResponse):
    async def aiter_content(self, chunk_size: int | None = None, decode_unicode: bool = False) -> AsyncIterator[bytes]:
        yield _PNG

    async def aclose(self) -> None:
        pass


class _WaitingSemaphore(asyncio.Semaphore):
    def __init__(self, entered: asyncio.Event) -> None:
        super().__init__(0)
        self.entered = entered

    async def acquire(self) -> bool:
        self.entered.set()
        return await super().acquire()


def _watch_web_close(web: WebClient, monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    closed = asyncio.Event()
    close = web._session.close

    async def record_close() -> None:
        await close()
        closed.set()

    monkeypatch.setattr(web._session, "close", record_close)
    return closed


def _image_transport(web: WebClient, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def request(method: str, url: str, **kwargs: object) -> CurlResponse:
        assert not web._session._closed
        response = _ImageResponse()
        response.url = url
        return response

    transport = AsyncMock(side_effect=request)
    monkeypatch.setattr(web._session, "request", transport)
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize(("waiting_at", "cancel"), [("resolve", False), ("semaphore", False), ("resolve", True)])
async def test_proxy_snapshot_survives_rebuild_before_first_request(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    public_dns: None,
    waiting_at: str,
    cancel: bool,
) -> None:
    runtime: AppRuntime = app.state.runtime
    old_web = runtime.web_client
    closed = _watch_web_close(old_web, monkeypatch)
    old_requests = _image_transport(old_web, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    url = "https://image.example/first.png"
    gate: _WaitingSemaphore | None = None
    if waiting_at == "resolve":
        resolve = runtime.resource_store.resolve

        async def paused_resolve(target: str) -> Path | None:
            if target == url and not release.is_set():
                entered.set()
                await release.wait()
            return await resolve(target)

        monkeypatch.setattr(runtime.resource_store, "resolve", paused_resolve)
    else:
        gate = _WaitingSemaphore(entered)
        monkeypatch.setattr(runtime, "proxy_downloads", gate)
    pending = asyncio.create_task(client.get("resources/proxy", params={"url": url}))
    try:
        await asyncio.wait_for(entered.wait(), LOAD_TOLERANT_TIMEOUT)
        changed = await asyncio.wait_for(
            client.patch("config", json={"watermark": {"enabled": True}}), LOAD_TOLERANT_TIMEOUT
        )
        assert changed.status_code == 200, changed.text
        assert runtime.web_client is not old_web
        old_requests.assert_not_awaited()
        assert not closed.is_set()
        new_requests = _image_transport(runtime.web_client, monkeypatch)
        if cancel:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            release.set()
            if gate is not None:
                gate.release()
            response = await asyncio.wait_for(pending, LOAD_TOLERANT_TIMEOUT)
            assert response.status_code == 200 and response.content == _PNG
            assert [call.args[0] for call in old_requests.await_args_list] == ["HEAD", "GET"]
        await asyncio.wait_for(closed.wait(), LOAD_TOLERANT_TIMEOUT)
        assert old_web._session._closed
        new_requests.assert_not_awaited()
        response = await client.get("resources/proxy", params={"url": "https://image.example/next.png"})
        assert response.status_code == 200 and response.content == _PNG
        assert [call.args[0] for call in new_requests.await_args_list] == ["HEAD", "GET"]
    finally:
        release.set()
        if gate is not None:
            gate.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


class _Body(httpx.AsyncByteStream):
    def __init__(self, waiting: asyncio.Event | None, release: asyncio.Event | None) -> None:
        self.waiting = waiting
        self.release = release

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"FIRST"
        if self.waiting is not None and self.release is not None:
            self.waiting.set()
            await self.release.wait()
        yield b"LAST"


class _MediaTransport(httpx.AsyncBaseTransport):
    def __init__(self, waiting: asyncio.Event | None = None, release: asyncio.Event | None = None) -> None:
        self.waiting = waiting
        self.release = release
        self.closed = asyncio.Event()
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert not self.closed.is_set()
        self.requests.append(request)
        headers = {"Content-Type": "video/mp4", "Content-Length": "9"}
        ranged = "Range" in request.headers
        if ranged:
            headers["Content-Range"] = "bytes 0-8/9"
        return httpx.Response(
            206 if ranged else 200,
            headers=headers,
            stream=_Body(self.waiting, self.release),
        )

    async def aclose(self) -> None:
        self.closed.set()


async def _replace_media_transport(factory: PlaybackFactory, transport: _MediaTransport) -> None:
    await factory.stream._client.aclose()
    factory.stream._client = httpx.AsyncClient(transport=transport)


async def _install_playback(app: FastAPI, client: httpx.AsyncClient, repo: Repository) -> int:
    runtime: AppRuntime = app.state.runtime
    write_plugin(runtime.config.cold.data_dir, _SOURCE, body=playback_plugin_source(_SOURCE))
    response = await client.post("plugins/reload")
    assert response.status_code == 200, response.text
    response = await client.patch(
        f"plugins/{_SOURCE}", json={"enabled": True, "config": {"url": "https://media.example/movie.mp4"}}
    )
    assert response.status_code == 200, response.text
    meta = await repo.upsert_metadata(number="LIFE-001", title="Lifetime")
    assert meta.id is not None
    return meta.id


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "resolve"])
async def test_playback_operation_started_before_stream_open_survives_rebuild(
    app: FastAPI,
    client: httpx.AsyncClient,
    repo: Repository,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    metadata_id = await _install_playback(app, client, repo)
    runtime: AppRuntime = app.state.runtime
    old_web = runtime.web_client
    web_closed = _watch_web_close(old_web, monkeypatch)
    factory = runtime.playback_factory
    assert factory is not None
    provider = factory.provider(_SOURCE)
    assert provider is not None
    transport = _MediaTransport()
    await _replace_media_transport(factory, transport)
    entered, release = asyncio.Event(), asyncio.Event()
    if operation == "probe":
        probe = provider.probe

        async def paused_probe(query: PlaybackQuery) -> tuple[PlaybackOffer, ...]:
            entered.set()
            await release.wait()
            return await probe(query)

        monkeypatch.setattr(provider, "probe", paused_probe)
    else:
        resolve = provider.resolve

        async def paused_resolve(query: PlaybackQuery) -> PlaybackTarget | None:
            entered.set()
            await release.wait()
            return await resolve(query)

        monkeypatch.setattr(provider, "resolve", paused_resolve)
    path = f"playback/{_SOURCE}/{metadata_id}" + ("/streams" if operation == "probe" else "")
    pending = asyncio.create_task(client.get(path))
    try:
        await asyncio.wait_for(entered.wait(), LOAD_TOLERANT_TIMEOUT)
        assert not transport.requests and factory.stream._in_flight == 0
        changed = await asyncio.wait_for(
            client.patch("config", json={"watermark": {"enabled": True}}), LOAD_TOLERANT_TIMEOUT
        )
        assert changed.status_code == 200, changed.text
        assert runtime.playback_factory is not factory
        assert not pending.done() and not transport.closed.is_set() and not web_closed.is_set()
        release.set()
        response = await asyncio.wait_for(pending, LOAD_TOLERANT_TIMEOUT)
        assert response.status_code == 200
        if operation == "probe":
            assert response.json()["items"][0]["available"] is True
            assert not transport.requests
        else:
            assert response.content == b"FIRSTLAST"
            assert len(transport.requests) == 1
        await asyncio.wait_for(transport.closed.wait(), LOAD_TOLERANT_TIMEOUT)
        await asyncio.wait_for(web_closed.wait(), LOAD_TOLERANT_TIMEOUT)
        assert factory.stream._client.is_closed and old_web._session._closed
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_playback_stream_holds_snapshot_until_response_body_finishes(
    app: FastAPI,
    client: httpx.AsyncClient,
    repo: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_id = await _install_playback(app, client, repo)
    runtime: AppRuntime = app.state.runtime
    factory = runtime.playback_factory
    assert factory is not None
    web_closed = _watch_web_close(runtime.web_client, monkeypatch)
    waiting, release = asyncio.Event(), asyncio.Event()
    old_transport = _MediaTransport(waiting, release)
    await _replace_media_transport(factory, old_transport)
    path = f"playback/{_SOURCE}/{metadata_id}"
    pending = asyncio.create_task(client.get(path, headers={"Range": "bytes=0-8"}))
    try:
        await asyncio.wait_for(waiting.wait(), LOAD_TOLERANT_TIMEOUT)
        assert factory.stream._in_flight == 1
        changed = await asyncio.wait_for(
            client.patch("config", json={"watermark": {"enabled": True}}), LOAD_TOLERANT_TIMEOUT
        )
        assert changed.status_code == 200, changed.text
        assert not pending.done() and not old_transport.closed.is_set() and not web_closed.is_set()
        replacement = runtime.playback_factory
        assert replacement is not None and replacement is not factory
        new_transport = _MediaTransport()
        await _replace_media_transport(replacement, new_transport)
        release.set()
        response = await asyncio.wait_for(pending, LOAD_TOLERANT_TIMEOUT)
        assert response.status_code == 206 and response.content == b"FIRSTLAST"
        assert response.headers["content-range"] == "bytes 0-8/9"
        assert old_transport.requests[0].headers["Range"] == "bytes=0-8"
        await asyncio.wait_for(old_transport.closed.wait(), LOAD_TOLERANT_TIMEOUT)
        await asyncio.wait_for(web_closed.wait(), LOAD_TOLERANT_TIMEOUT)
        assert factory.stream._client.is_closed and factory.stream._in_flight == 0
        response = await client.get(path)
        assert response.status_code == 200 and response.content == b"FIRSTLAST"
        assert len(old_transport.requests) == len(new_transport.requests) == 1
        assert not new_transport.closed.is_set()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
