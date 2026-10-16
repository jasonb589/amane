"""通知: 任务完成 / 失败时按渠道模板推出去.

入口只有 ``on_event``: 由 ``EventBus`` 的监听器在 emit 之后调用, 因此**绝不能把异常抛回**
任务链路 (见 ``EventBus._notify_listeners``). 实际发送放在后台 task 里, 不阻塞 worker.

变量取自任务记录 (``repo.get_task``): 类型 / 状态 / 耗时 / 错误 / payload 里的番号与库,
再按需补标题 (``get_metadata_by_number``) 与任务日志尾部.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..config.manager import NotifyChannelConfig, NotifyConfig
from ..events import Event, EventType
from ..net.errors import RequestError
from ..parsing import parse_file_info
from .templates import render

if TYPE_CHECKING:
    from ..db.repository import Repository
    from ..net.http import WebClient

logger = structlog.get_logger()

_TERMINAL = frozenset({EventType.TASK_COMPLETED, EventType.TASK_FAILED})

#: 任务类型 → 通知里的标签 (与面板用词一致).
_TYPE_LABELS: dict[str, str] = {
    "scrape": "刮削",
    "organize": "整理",
    "refresh": "刷新",
    "cleanup": "清理",
    "upscale": "超分",
    "r18_import": "R18 导入",
    "actor_scrape": "演员刮削",
    "rescrape": "重新刮削",
    "scan_invalid": "扫描无效文件",
    "delete": "删除",
}


def _format_duration(started: datetime | None, finished: datetime | None) -> str:
    if started is None or finished is None:
        return ""
    seconds = max(0.0, (finished - started).total_seconds())
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


@dataclass
class _Pending:
    """合并窗口内累积的同渠道同类事件."""

    variables: dict[str, Any]
    numbers: list[str] = field(default_factory=list)
    count: int = 0
    timer: asyncio.Task[None] | None = None


class NotificationService:
    """按 ``notify`` 配置推送任务终态; 未启用时零开销."""

    def __init__(self) -> None:
        self._config: NotifyConfig | None = None
        self._web: WebClient | None = None
        self._repo: Repository | None = None
        self._log_dir: Path | None = None
        self._pending: dict[tuple[int, bool], _Pending] = {}
        self._sending: set[asyncio.Task[None]] = set()

    def configure(
        self,
        config: NotifyConfig,
        *,
        web_client: WebClient | None,
        repo: Repository | None,
        log_dir: Path | None,
    ) -> None:
        """热配置变更时调用 (同步; rebuild 不能 await): 只换引用, 在途发送继续跑完."""
        self._config = config.model_copy(deep=True)
        self._web = web_client
        self._repo = repo
        self._log_dir = log_dir

    @property
    def enabled(self) -> bool:
        return bool(
            self._config is not None
            and self._config.enabled
            and self._config.channels
            and self._web is not None
            and self._repo is not None
        )

    async def close(self) -> None:
        """停服: 取消还没到点的合并窗口 (在途发送不打断)."""
        for pending in self._pending.values():
            if pending.timer is not None:
                pending.timer.cancel()
        self._pending.clear()

    # ------------------------------------------------------------------ 事件

    async def on_event(self, event: Event) -> None:
        """EventBus 监听器; 任何失败都只记日志, 不向任务链路抛."""
        if event.type not in _TERMINAL or not self.enabled:
            return
        config = self._config
        assert config is not None
        try:
            variables = await self._variables(event, config)
        except Exception:
            logger.exception("notify context failed", event_type=str(event.type))
            return
        failed = event.type is EventType.TASK_FAILED
        for index, channel in enumerate(config.channels):
            if not self._matches(channel, event, variables):
                continue
            self._dispatch(index, channel, failed, variables)

    def _matches(self, channel: NotifyChannelConfig, event: Event, variables: dict[str, Any]) -> bool:
        if not channel.enabled or not channel.url:
            return False
        if channel.events and str(event.type) not in channel.events:
            return False
        return not (channel.task_types and str(variables.get("type", "")) not in channel.task_types)

    def _dispatch(self, index: int, channel: NotifyChannelConfig, failed: bool, variables: dict[str, Any]) -> None:
        """throttle=0 直接发; 否则攒进窗口, 窗口结束发一条汇总."""
        if channel.throttle_seconds <= 0:
            self._spawn(channel, variables)
            return
        key = (index, failed)
        pending = self._pending.get(key)
        if pending is None:
            pending = _Pending(variables=variables)
            self._pending[key] = pending
            pending.timer = asyncio.create_task(self._flush_later(index, channel, failed))
        pending.count += 1
        number = str(variables.get("number") or "")
        if number and number not in pending.numbers:
            pending.numbers.append(number)

    async def _flush_later(self, index: int, channel: NotifyChannelConfig, failed: bool) -> None:
        await asyncio.sleep(channel.throttle_seconds)
        pending = self._pending.pop((index, failed), None)
        if pending is None:
            return
        variables = dict(pending.variables)
        variables["count"] = max(pending.count, 1)
        variables["numbers"] = ", ".join(pending.numbers)
        self._spawn(channel, variables)

    def _spawn(self, channel: NotifyChannelConfig, variables: dict[str, Any]) -> None:
        task = asyncio.create_task(self._send(channel, variables))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _send(self, channel: NotifyChannelConfig, variables: dict[str, Any]) -> None:
        web = self._web
        if web is None:  # pragma: no cover - enabled 已保证非空
            return
        label = channel.name or channel.url
        body = render(channel.body, variables, json_mode=channel.body.lstrip().startswith("{"))
        url = render(channel.url, variables)
        try:
            response = await web.request(
                channel.method.value.upper(),
                url,
                headers=dict(channel.headers) or None,
                data=body or None,
                timeout=channel.timeout,
            )
        except RequestError as exc:
            logger.warning("notify request failed", channel=label, error=str(exc))
            return
        if response.status_code >= 400:
            logger.warning(
                "notify rejected",
                channel=label,
                status=response.status_code,
                body=response.text[:200],
            )

    # ------------------------------------------------------------------ 变量

    async def _variables(self, event: Event, config: NotifyConfig) -> dict[str, Any]:
        repo = self._repo
        raw_id = event.data.get("task_id")
        task = None
        if repo is not None and isinstance(raw_id, int):
            task = await repo.get_task(raw_id)
        payload: dict[str, Any] = dict(task.payload or {}) if task is not None else {}
        task_type = str(task.type) if task is not None else str(event.data.get("type") or "")
        number = await self._number(repo, payload)
        title = ""
        if number and repo is not None:
            metadata = await repo.get_metadata_by_number(number)
            title = (metadata.title or "") if metadata is not None else ""
        library = ""
        library_id = payload.get("library_id")
        if repo is not None and isinstance(library_id, int):
            found = await repo.get_library(library_id)
            library = found.name if found is not None else ""
        failed = event.type is EventType.TASK_FAILED
        error = (task.error or "") if task is not None else ""
        if not error:
            error = str(event.data.get("error") or "")
        finished = task.finished_at.isoformat() if task is not None and task.finished_at else ""
        return {
            "task_id": raw_id if isinstance(raw_id, int) else "",
            "type": task_type,
            "type_label": _TYPE_LABELS.get(task_type, task_type),
            "status": "失败" if failed else "成功",
            "success": not failed,
            "error": error,
            "duration": _format_duration(task.started_at, task.finished_at) if task is not None else "",
            "number": number,
            "title": title,
            "library": library,
            "finished_at": finished,
            "link": f"{config.panel_url.rstrip('/')}/tasks" if config.panel_url else "",
            "log_tail": await self._log_tail(raw_id, config.log_tail_lines) if config.log_tail_lines > 0 else "",
            "count": 1,
            "numbers": number,
        }

    async def _number(self, repo: Repository | None, payload: dict[str, Any]) -> str:
        """番号取 payload 的 ``number``; 没有就从路径或关联媒体文件反推."""
        direct = payload.get("number")
        if isinstance(direct, str) and direct:
            return direct
        path = payload.get("path") or payload.get("file_path") or payload.get("scope_path")
        if isinstance(path, str) and path:
            parsed = parse_file_info(path).number
            if parsed:
                return parsed
        ids = payload.get("media_file_ids")
        if repo is not None and isinstance(ids, list) and ids and isinstance(ids[0], int):
            media = await repo.get_media_file(ids[0])
            if media is not None:
                parsed = parse_file_info(media.path).number
                if parsed:
                    return parsed
        return ""
__all__ = ["NotificationService"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = text.splitlines()[-lines:]
    return "\n".join(tail)


__all__ = ["NotificationService"]
