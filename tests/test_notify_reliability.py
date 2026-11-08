"""通知边界: 格式、业务拒绝、限流、热重载与关闭生命周期."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from curl_cffi import Response

from amane.config.manager import NotifyChannelConfig, NotifyConfig, NotifyProvider, NotifyTaskType
from amane.db.models import TaskType
from amane.events import Event, EventType
from amane.net.errors import RequestError
from amane.net.http import RateLimiters, WebClient
from amane.notify.service import NotificationService, _local_time, _read_tail, build_request
from amane.notify.telegram import clip_html, plain_html, text_length
from amane.notify.templates import render
from amane.parsing import ContentType
from tests.helpers import LOAD_TOLERANT_TIMEOUT


def _channel(**values: Any) -> NotifyChannelConfig:
    return NotifyChannelConfig(provider=NotifyProvider.TELEGRAM, token="123:private", chat_id="-1001", **values)


class _Response:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status_code = status
        self.text = json.dumps({"ok": True} if body is None else body)


class _Web:
    def __init__(self, *responses: _Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def lease(self) -> nullcontext[None]:
        return nullcontext()

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        result = self.responses.pop(0) if self.responses else _Response()
        if isinstance(result, Exception):
            raise result
        return result


def _service(web: _Web, channel: NotifyChannelConfig) -> NotificationService:
    service = NotificationService()
    service.configure(
        NotifyConfig(enabled=True, channels=[channel]),
        web_client=web,  # type: ignore[arg-type]
        repo=None,
        log_dir=None,
    )
    return service


@pytest.mark.parametrize("mode", [{}, {"html_mode": True}, {"json_mode": True}, {"markdown_mode": True}])
def test_variable_content_is_never_evaluated_as_a_template(mode: dict[str, bool]) -> None:
    result = render("{{title}} {number}", {"title": "{token}", "number": "ABC-1", "token": "PRIVATE"}, **mode)
    assert "PRIVATE" not in result
    assert "token" in result


def test_markdown_escapes_variable_punctuation() -> None:
    assert render("*{title}*", {"title": "A_[B]."}, markdown_mode=True) == r"*A\_\[B\]\." + "*"
    assert render("{title|r}", {"title": "*raw*"}, markdown_mode=True) == "*raw*"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("<b>abcdefghij</b>", 6, "<b>abcde…</b>"),
        ("<b>A &amp; B &lt;C&gt;</b>", 5, "<b>A &amp; …</b>"),
        ("<blockquote><b>😀😀😀</b></blockquote>", 5, "<blockquote><b>😀😀…</b></blockquote>"),
        ("<i>abc</i>", 3, "<i>abc</i>"),
        ("<b>abc", 3, "<b>abc</b>"),
        ("<b>abc<i>def</b>", 6, "<b>abc<i>def</i></b>"),
    ],
)
def test_html_limits_preserve_entities_and_balanced_tags(text: str, limit: int, expected: str) -> None:
    result = clip_html(text, limit)
    assert result == expected
    assert text_length(plain_html(result)) <= limit


def test_photo_caption_and_batch_summary_limits() -> None:
    channel = _channel(send_photo=True, parse_mode=" html ", text="<b>{title}</b>")
    _, _, body = build_request(channel, {"title": "😀" * 2000, "poster_url": "https://image.test/p.jpg"})
    caption = json.loads(body)["caption"]
    assert caption.endswith("…</b>")
    assert text_length(plain_html(caption)) <= 1024
    channel.text = ""
    url, _, body = build_request(
        channel, {"count": 3, "poster_url": "https://image.test/p.jpg", "batch_items": "ABC-1\nABC-2"}
    )
    assert url.endswith("sendMessage")
    assert "ABC-1\nABC-2" in json.loads(body)["text"]


def test_telegram_options_and_internal_resource_query() -> None:
    channel = _channel(send_photo=True, message_thread_id=12, disable_notification=True, text=r"{number}\n{title}")
    _, _, body = build_request(
        channel,
        {
            "poster_url": "/api/resources/key?size=small",
            "number": "ABC-1",
            "title": "标题",
            "link": "https://panel.test/tasks",
        },
        panel_url="https://panel.test",
        image_token="a+b",
    )
    payload = json.loads(body)
    assert payload["caption"] == "ABC-1\n标题"
    assert payload["photo"] == "https://panel.test/api/resources/key?size=small&token=a%2Bb"
    assert payload["message_thread_id"] == 12
    assert payload["disable_notification"] is True
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://panel.test/tasks"


@pytest.mark.parametrize("options", [{"message_thread_id": -1}, {"parse_mode": "unknown"}])
def test_invalid_telegram_options_are_rejected(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=next(iter(options))):
        _channel(**options)


def test_notification_task_filter_covers_runtime_tasks() -> None:
    assert {item.value for item in NotifyTaskType} == {item.value for item in TaskType}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,body",
    [
        ("telegram", {"ok": False, "description": "denied"}),
        ("wecom", {"errcode": 93000, "errmsg": "denied"}),
        ("bark", {"code": 400, "message": "denied"}),
    ],
)
async def test_business_error_is_not_reported_as_success(provider: str, body: dict[str, Any]) -> None:
    web = _Web(_Response(200, body))
    channel = NotifyChannelConfig(provider=NotifyProvider(provider), token="private", chat_id="c")
    ok, message = await _service(web, channel).send_test(channel)
    assert not ok
    assert "denied" in message
    assert len(web.calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_obeys_retry_after_and_has_one_retry_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeper = AsyncMock()
    monkeypatch.setattr("amane.notify.service.asyncio.sleep", sleeper)
    web = _Web(_Response(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 9}}), _Response())
    channel = _channel(method="get")
    assert (await _service(web, channel).send_test(channel))[0]
    sleeper.assert_awaited_once_with(9)
    assert len(web.calls) == 2
    assert all(
        call["max_attempts"] == 1 and call["method"] == "POST" and not call["allow_redirects"] for call in web.calls
    )


@pytest.mark.asyncio
async def test_reconfigure_keeps_retired_client_until_rate_limit_retry_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_web = WebClient(limiters=RateLimiters(default_rate=10000))
    new_web = WebClient(limiters=RateLimiters(default_rate=10000))
    retry_waiting, release_retry, old_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    limited = Response()
    limited.status_code = 429
    limited.content = b'{"ok":false,"error_code":429,"parameters":{"retry_after":1}}'
    delivered = Response()
    delivered.content = b'{"ok":true}'
    old_request = AsyncMock(side_effect=[limited, delivered])
    new_request = AsyncMock(return_value=delivered)

    async def close_old() -> None:
        old_closed.set()

    async def retry_delay(delay: float) -> None:
        assert delay == 1
        retry_waiting.set()
        await release_retry.wait()
        assert not old_closed.is_set()

    monkeypatch.setattr(old_web._session, "request", old_request)
    monkeypatch.setattr(new_web._session, "request", new_request)
    monkeypatch.setattr(old_web._session, "close", close_old)
    monkeypatch.setattr(new_web._session, "close", AsyncMock())
    monkeypatch.setattr("amane.notify.service.asyncio.sleep", retry_delay)
    channel = _channel()
    config = NotifyConfig(enabled=True, channels=[channel])
    service = NotificationService()
    service.configure(config, web_client=old_web, repo=None, log_dir=None)
    sending = asyncio.create_task(service.send_test(channel))
    try:
        await asyncio.wait_for(retry_waiting.wait(), LOAD_TOLERANT_TIMEOUT)
        service.configure(config, web_client=new_web, repo=None, log_dir=None)
        old_web.retire()
        assert not old_closed.is_set()
        release_retry.set()
        assert (await asyncio.wait_for(sending, LOAD_TOLERANT_TIMEOUT))[0]
        await asyncio.wait_for(old_closed.wait(), LOAD_TOLERANT_TIMEOUT)
        assert old_request.await_count == 2
        new_request.assert_not_awaited()
        assert (await service.send_test(channel))[0]
        new_request.assert_awaited_once()
    finally:
        release_retry.set()
        await asyncio.gather(sending, return_exceptions=True)
        await service.close()
        await old_web.close()
        await new_web.close()


@pytest.mark.asyncio
async def test_queued_notification_uses_client_selected_after_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    old_web = WebClient(limiters=RateLimiters(default_rate=10000))
    new_web = WebClient(limiters=RateLimiters(default_rate=10000))
    delivered = Response()
    delivered.content = b'{"ok":true}'
    old_request, new_request = AsyncMock(return_value=delivered), AsyncMock(return_value=delivered)
    monkeypatch.setattr(old_web._session, "request", old_request)
    monkeypatch.setattr(new_web._session, "request", new_request)
    monkeypatch.setattr(old_web._session, "close", AsyncMock())
    monkeypatch.setattr(new_web._session, "close", AsyncMock())
    channel = _channel()
    config = NotifyConfig(enabled=True, channels=[channel])
    service = NotificationService()
    service.configure(config, web_client=old_web, repo=None, log_dir=None)
    for _ in range(4):
        await service._slots.acquire()
    queued = asyncio.create_task(service.send_test(channel))
    try:
        await asyncio.sleep(0)
        assert not queued.done()
        service.configure(config, web_client=new_web, repo=None, log_dir=None)
        old_web.retire()
        await asyncio.wait_for(old_web.close(), LOAD_TOLERANT_TIMEOUT)
        service._slots.release()
        assert (await asyncio.wait_for(queued, LOAD_TOLERANT_TIMEOUT))[0]
        old_request.assert_not_awaited()
        new_request.assert_awaited_once()
    finally:
        service._slots.release()
        await asyncio.gather(queued, return_exceptions=True)
        await service.close()
        await old_web.close()
        await new_web.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,description,count",
    [
        (400, "Bad Request: failed to get HTTP URL content", 2),
        (403, "Forbidden: bot blocked", 1),
        (429, "Too Many Requests", 2),
    ],
)
async def test_photo_fallback_only_after_explicit_image_rejection(
    monkeypatch: pytest.MonkeyPatch, status: int, description: str, count: int
) -> None:
    monkeypatch.setattr("amane.notify.service.asyncio.sleep", AsyncMock())
    web = _Web(_Response(status, {"ok": False, "error_code": status, "description": description}), _Response())
    channel = _channel(send_photo=True, text="{number}")
    await _service(web, channel)._send(channel, {"number": "ABC-1", "poster_url": "https://image.test/p.jpg"})
    assert len(web.calls) == count
    assert web.calls[-1]["url"].endswith("sendMessage" if status == 400 else "sendPhoto")


@pytest.mark.asyncio
async def test_html_rejection_retries_plain_text() -> None:
    web = _Web(_Response(400, {"ok": False, "description": "Bad Request: can't parse entities"}), _Response())
    channel = _channel(parse_mode="HTML", text="<b>{title}</b>")
    assert (await _service(web, channel).send_test(channel))[0]
    payload = json.loads(web.calls[-1]["data"])
    assert "parse_mode" not in payload
    assert "<b>" not in payload["text"]


@pytest.mark.asyncio
async def test_uncertain_transport_failure_is_not_resent_and_hides_token() -> None:
    channel = _channel(send_photo=True)
    web = _Web(RequestError("https://api.telegram.org/bot123:private/sendPhoto", "timeout 123:private"))
    ok, message = await _service(web, channel)._send(channel, {"poster_url": "https://image.test/p.jpg"})
    assert not ok and "private" not in message
    assert len(web.calls) == 1


@pytest.mark.asyncio
async def test_reconfigure_discards_old_windows_and_separates_task_types() -> None:
    channel = _channel(throttle_seconds=60)
    web = _Web()
    service = _service(web, channel)
    for task_type in ("scrape", "organize"):
        service._dispatch(0, channel, False, {"type": task_type, "number": "ABC-1"})
    assert len(service._pending) == 2
    timers = [pending.timer for pending in service._pending.values() if pending.timer]
    service.configure(NotifyConfig(enabled=False), web_client=web, repo=None, log_dir=None)  # type: ignore[arg-type]
    assert not service._pending
    await asyncio.gather(*timers, return_exceptions=True)
    assert not web.calls
    await service.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_an_active_delivery() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class PausedWeb(_Web):
        async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
            entered.set()
            await release.wait()
            return await super().request(method, url, **kwargs)

    channel = _channel()
    web = PausedWeb()
    service = _service(web, channel)
    service._spawn(channel, {"number": "ABC-1"})
    await asyncio.wait_for(entered.wait(), 2)
    closer = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not closer.done()
    release.set()
    await asyncio.wait_for(closer, 2)
    assert not service._sending
    assert len(web.calls) == 1


@pytest.mark.asyncio
async def test_file_event_and_metadata_content_type_control_filtering(repo: Any) -> None:
    await repo.upsert_metadata("ABC-123", content_type=ContentType.HENTAI, title="标题")
    channel = _channel(events=["file_discovered"], content_types=["hentai"], text="{type_label} {number}")
    web = _Web()
    service = _service(web, channel)
    service.configure(NotifyConfig(enabled=True, channels=[channel]), web_client=web, repo=repo, log_dir=None)  # type: ignore[arg-type]
    await service.on_event(Event(type=EventType.FILE_DISCOVERED, data={"path": "ABC-123.mp4"}))
    await service.close()
    assert len(web.calls) == 1
    assert json.loads(web.calls[0]["data"])["text"] == "文件发现 ABC-123"


def test_log_tail_is_bounded_and_respects_empty_request(tmp_path: Path) -> None:
    path = tmp_path / "task.log"
    path.write_text("x" * 400_000 + "\n一\n二\n三\n", encoding="utf-8")
    assert _read_tail(path, 2) == "二\n三"
    assert _read_tail(path, 0) == ""
    assert _read_tail(tmp_path / "absent", 2) == ""


def test_aware_local_time_keeps_original_instant() -> None:
    value = datetime(2026, 11, 7, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert _local_time(value) == value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
