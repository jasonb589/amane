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
from amane.notify.service import NotificationService

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
