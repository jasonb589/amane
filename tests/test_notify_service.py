"""通知服务的行为自检: 事件 → 变量 → 模板 → 发送 (本 fork 新增).

用桩对象替代 Repository / WebClient, 只验证本功能的链路: 变量取值、JSON 自动转义、
面板链接、日志尾部, 以及未启用时不发任何请求.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from amane.config.manager import NotifyChannelConfig, NotifyConfig
from amane.db.models import Task, TaskType
from amane.events import Event, EventType
from amane.notify.service import NotificationService, _batch_line, _local_time, build_request

_STARTED = datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC)


class _StubRepo:
    """只实现通知用到的读接口."""

    def __init__(self, task: Task, *, title: str = "标题 A", library: str = "正常") -> None:
        self._task = task
        self._title = title
        self._library = library

    async def get_task(self, task_id: int) -> Task | None:
        return self._task if task_id == self._task.id else None

    async def get_metadata_by_number(self, number: str) -> Any:
        del number
        return type(
            "Meta",
            (),
            {
                "title": self._title,
                "poster_url": "https://img.test/poster.jpg",
                "poster_urls": ["https://img.test/poster.jpg"],
                "thumb_url": "",
                "thumb_urls": [],
                "plot": "剧情文本",
                "tags": ["标签A", "标签B"],
                "actors": ["演员A"],
                "studio": "片商",
                "release": "2026-01-01",
            },
        )()

    async def get_library(self, library_id: int) -> Any:
        del library_id
        return type("Lib", (), {"name": self._library})()

    async def get_media_file(self, media_id: int) -> Any:
        del media_id
        return None


class _StubWeb:
    """记录请求, 不真的发出去."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        return type("Resp", (), {"status_code": 200, "text": ""})()


def _task() -> Task:
    return Task(
        id=7,
        type=TaskType.SCRAPE,
        payload={"number": "ABC-001", "library_id": 3},
        error=None,
        started_at=_STARTED,
        finished_at=_STARTED + timedelta(seconds=83),
    )


async def _settle(web: _StubWeb) -> None:
    for _ in range(50):
        if web.calls:
            return
        await asyncio.sleep(0.01)


def test_service_renders_and_sends(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    (log_dir / "tasks" / "task-7").mkdir(parents=True)
    (log_dir / "tasks" / "task-7" / "task.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    channel = NotifyChannelConfig.model_validate(
        {
            "name": "stub",
            "url": "https://example.test/{number}",
            "body": '{"text":"{type_label} {status} {number} {title} {library} {duration} {count} {link} poster={poster_url} tail={log_tail}"}',
        }
    )
    config = NotifyConfig(enabled=True, panel_url="https://amane.test/", log_tail_lines=2, channels=[channel])
    web = _StubWeb()

    async def run() -> None:
        service = NotificationService()
        # 桩对象只为验证链路, 不是真的 WebClient / Repository.
        service.configure(config, web_client=web, repo=_StubRepo(_task()), log_dir=log_dir)  # type: ignore[arg-type]
        await service.on_event(Event(type=EventType.TASK_COMPLETED, data={"task_id": 7}))
        await _settle(web)
        await service.close()

    asyncio.run(run())
    assert len(web.calls) == 1
    call = web.calls[0]
    assert call["method"] == "POST"
    # JSON 模板自动补 Content-Type, 否则对方按表单解析 → Telegram 会回 "message text is empty".
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["url"] == "https://example.test/ABC-001"
    body = call["data"]
    # JSON 模式: 日志尾部的换行被转义成 \n, 面板链接来自 panel_url.
    tail = json.dumps("line2\nline3", ensure_ascii=False)[1:-1]
    assert (
        f'"刮削 成功 ABC-001 标题 A 正常 1m23s 1 https://amane.test/tasks poster=https://img.test/poster.jpg tail={tail}"'
        in body
    )


def test_service_skips_when_disabled() -> None:
    web = _StubWeb()

    async def run() -> None:
        service = NotificationService()
        service.configure(NotifyConfig(enabled=False), web_client=web, repo=_StubRepo(_task()), log_dir=None)  # type: ignore[arg-type]
        await service.on_event(Event(type=EventType.TASK_COMPLETED, data={"task_id": 7}))
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert web.calls == []


def test_local_time_formats_container_timezone() -> None:
    """通知里的本地时间: 库里是 UTC naive, 显示时按容器时区 (compose 的 TZ) 转一次."""
    text = _local_time(datetime(2026, 10, 16, 12, 44, 36))  # noqa: DTZ001 — 库里存的是 UTC naive
    assert len(text) == 19 and text[4] == "-" and text[13] == ":"
    assert text.startswith("2026-10-16 ")


class _NotifyResponse:
    status_code = 200
    text = ""


class _RecordingWeb:
    """只记录请求体的 WebClient 桩 (合并逻辑单测用)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> _NotifyResponse:
        self.calls.append(str(kwargs.get("data") or ""))
        return _NotifyResponse()


def test_batch_line_summarises_numbers() -> None:
    """只有一项时不显示摘要行; 多项时列出番号, 超过上限折成" 等 N 个"."""
    assert _batch_line(1, ["ABC-1"]) == ""
    assert _batch_line(3, ["ABC-1", "ABC-2", "ABC-3"]) == "\n共 3 项：ABC-1、ABC-2、ABC-3"
    assert _batch_line(2, []) == "\n共 2 项"
    assert _batch_line(12, [f"A-{i}" for i in range(12)], limit=3) == "\n共 12 项：A-0、A-1、A-2 等 12 个"


def test_quiet_window_merges_events_into_one_summary() -> None:
    """静默窗口: 一批任务的多个终态只发一条, 正文带 {count} / {numbers} / {batch}."""
    web = _RecordingWeb()
    service = NotificationService()
    channel = NotifyChannelConfig(
        name="t",
        url="https://example.com/hook",
        body='{"n":{count},"numbers":"{numbers}","b":"{batch}"}',
        throttle_seconds=1,
        max_wait_seconds=30,
    )
    service.configure(NotifyConfig(enabled=True, channels=[channel]), web_client=web, repo=None, log_dir=None)

    async def run() -> None:
        for number in ("ABC-1", "ABC-2", "ABC-3"):
            service._dispatch(0, channel, False, {"number": number, "count": 1, "numbers": number, "batch": ""})
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.6)
        await asyncio.sleep(0.2)
        assert len(web.calls) == 1
        body = web.calls[0]
        assert '"n":3' in body
        assert "ABC-1、ABC-2、ABC-3" in body
        assert "共 3 项" in body

    asyncio.run(run())


def test_max_wait_flushes_while_events_keep_arriving() -> None:
    """上限: 事件一直不断时也要先发一条, 不能无限等静默."""
    web = _RecordingWeb()
    service = NotificationService()
    channel = NotifyChannelConfig(
        name="t",
        url="https://example.com/hook",
        body='{"n":{count}}',
        throttle_seconds=2,
        max_wait_seconds=1,
    )
    service.configure(NotifyConfig(enabled=True, channels=[channel]), web_client=web, repo=None, log_dir=None)

    async def run() -> None:
        for _ in range(5):
            service._dispatch(0, channel, False, {"count": 1, "numbers": "", "batch": ""})
            await asyncio.sleep(0.35)
            if web.calls:
                break
        assert web.calls, "到达合并上限后应当已经发送"

    asyncio.run(run())


def test_build_request_telegram_text() -> None:
    """telegram: 只填 token/chat_id/正文, 程序拼出 sendMessage 与 JSON."""
    channel = NotifyChannelConfig(
        provider="telegram", token="123:abc", chat_id="-1001", text="{type_label} {status}\n{number}"
    )
    url, headers, body = build_request(channel, {"type_label": "刮削", "status": "成功", "number": "ABC-1"})
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["text"] == "刮削 成功\nABC-1"


def test_build_request_telegram_photo() -> None:
    """send_photo 打开且有海报时走 sendPhoto; 没有海报时退回 sendMessage (避免必然 400)."""
    channel = NotifyChannelConfig(provider="telegram", token="t", chat_id="c", text="{number}", send_photo=True)
    url, _, body = build_request(channel, {"number": "ABC-1", "poster_url": "https://img/x.jpg"})
    assert url.endswith("/sendPhoto")
    payload = json.loads(body)
    assert payload["photo"] == "https://img/x.jpg"
    assert payload["caption"] == "ABC-1"
    url2, _, body2 = build_request(channel, {"number": "ABC-1", "poster_url": ""})
    assert url2.endswith("/sendMessage")
    assert json.loads(body2)["text"] == "ABC-1"


def test_build_request_bark_and_wecom() -> None:
    """Bark 用第一行当标题; 企业微信发 text 消息."""
    bark = NotifyChannelConfig(provider="bark", token="key1", text="标题行\n正文行")
    url, _, body = build_request(bark, {})
    assert url == "https://api.day.app/push"
    assert json.loads(body) == {"device_key": "key1", "title": "标题行", "body": "正文行"}
    wecom = NotifyChannelConfig(provider="wecom", token="k2", text="{number}")
    url, _, body = build_request(wecom, {"number": "ABC-2"})
    assert url.endswith("webhook/send?key=k2")
    assert json.loads(body) == {"msgtype": "text", "text": {"content": "ABC-2"}}


def test_build_request_default_text_and_custom_passthrough() -> None:
    """text 留空用内置默认正文; custom 渠道仍按 url/body 原样渲染 (老配置不受影响)."""
    preset = NotifyChannelConfig(provider="telegram", token="t", chat_id="c")
    _, _, body = build_request(preset, {"type_label": "整理", "status": "成功", "number": "A-1"})
    assert "整理 成功" in json.loads(body)["text"]
    custom = NotifyChannelConfig(url="https://x/{number}", body='{"t":"{number}"}')
    url, _, body = build_request(custom, {"number": "A-2"})
    assert url == "https://x/A-2"
    assert body == '{"t":"A-2"}'


def test_send_test_reports_result() -> None:
    """测试发送: 成功给 HTTP 码; 缺 token 时不发请求, 直接返回可读原因."""
    web = _RecordingWeb()
    service = NotificationService()
    service.configure(NotifyConfig(enabled=True), web_client=web, repo=None, log_dir=None)
    channel = NotifyChannelConfig(provider="telegram", token="t", chat_id="c", text="{number}")
    broken = NotifyChannelConfig(provider="telegram", chat_id="c", text="x")

    async def run() -> None:
        ok, message = await service.send_test(channel)
        assert ok is True
        assert "200" in message
        assert web.calls  # 真的发了请求
        ok2, message2 = await service.send_test(broken)
        assert ok2 is False
        assert "Token" in message2

    asyncio.run(run())


def test_matches_accepts_preset_channel_without_url() -> None:
    """回归: 预设渠道 (provider != custom) 即使不填 url 也必须能匹配事件.

    以前的 _matches 要求 url 非空, 于是预设渠道把所有真实事件都静默丢掉了
    (界面上的"测试发送"不走这里, 所以当时测起来是好的).
    """
    service = NotificationService()
    event = Event(type=EventType.TASK_COMPLETED, data={"task_id": 1})
    ok = NotifyChannelConfig(provider="telegram", token="t", chat_id="c")
    assert service._matches(ok, event, {"type": "scrape", "category": "censored"}) is True
    # 预设渠道缺 token: 根本发不出去, 不该匹配
    assert service._matches(NotifyChannelConfig(provider="telegram"), event, {"type": "scrape"}) is False
    # 自定义 HTTP 仍然要求 url
    assert service._matches(NotifyChannelConfig(), event, {"type": "scrape"}) is False
