"""通知: 任务完成 / 失败时按渠道模板推出去.

入口只有 ``on_event``: 由 ``EventBus`` 的监听器在 emit 之后调用, 因此**绝不能把异常抛回**
任务链路 (见 ``EventBus._notify_listeners``). 实际发送放在后台 task 里, 不阻塞 worker.

变量取自任务记录 (``repo.get_task``): 类型 / 状态 / 耗时 / 错误 / payload 里的番号与库,
再按需补标题 (``get_metadata_by_number``) 与任务日志尾部.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..config.manager import NotifyChannelConfig, NotifyConfig
from ..events import Event, EventType
from ..net.errors import RequestError
from ..parsing import infer_content_type, parse_file_info
from .templates import render

if TYPE_CHECKING:
    from ..db.repository import Repository
    from ..net.http import WebClient

logger = structlog.get_logger()

_TERMINAL = frozenset({EventType.TASK_COMPLETED, EventType.TASK_FAILED})

#: ``EventType`` → 配置里的短取值 (``NotifyEvent``, 不带点以便 i18n 选项能查到翻译).
_EVENT_KEYS: dict[EventType, str] = {
    EventType.TASK_COMPLETED: "completed",
    EventType.TASK_FAILED: "failed",
    EventType.FILE_DISCOVERED: "file_discovered",
    EventType.FILE_REMOVED: "file_removed",
}

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


#: 内容类型 → 中文标签 (与设置页「海报裁剪方式」里的叫法一致).
_CATEGORY_LABELS: dict[str, str] = {
    "censored": "有码",
    "uncensored": "无码",
    "chinese": "国产",
    "western": "欧美",
    "fc2": "FC2",
    "amateur": "素人",
    "hentai": "动漫",
}


def _actor_name(item: Any) -> str:
    """出演者可能是 ``FilmActor`` / dict / 字符串 (旧数据), 统一取名字."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("name") or "")
    return str(getattr(item, "name", "") or "")


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


def _local_time(value: datetime) -> str:
    """库里存的是 UTC naive 时间, 通知里按容器时区显示 (compose 的 TZ 决定)."""
    return value.replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


    """合并成多条时的摘要行: 以换行开头, 只有一项时返回空串 (模板里那一段自然消失)."""
    if count <= 1:
        return ""
    text = f"共 {count} 项"
    if numbers:
        shown = "、".join(numbers[:limit])
        if len(numbers) > limit:
            shown = f"{shown} 等 {len(numbers)} 个"
        text = f"{text}：{shown}"
    return "\n" + text


@dataclass
class _Pending:
    """合并窗口内累积的同渠道同类事件."""

    variables: dict[str, Any]
    numbers: list[str] = field(default_factory=list)
    count: int = 0
    first_at: float = 0.0
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
        if channel.events and _EVENT_KEYS.get(event.type, "") not in {str(item) for item in channel.events}:
            return False
        selected_types = {str(item) for item in channel.task_types}
        if selected_types and str(variables.get("type", "")) not in selected_types:
            return False
        selected_categories = {str(item) for item in channel.content_types}
        if selected_categories and str(variables.get("category", "")) not in selected_categories:
            return False
        return all(bool(variables.get(name)) for name in channel.require)

    def _dispatch(self, index: int, channel: NotifyChannelConfig, failed: bool, variables: dict[str, Any]) -> None:
        """throttle=0 直接发; 否则把连续事件攒成一条, 静默 N 秒后发送 (最长等 max_wait_seconds)."""
        if channel.throttle_seconds <= 0:
            self._spawn(channel, variables)
            return
        key = (index, failed)
        pending = self._pending.get(key)
        if pending is None:
            pending = _Pending(variables=variables, first_at=time.monotonic())
            self._pending[key] = pending
        else:
            # 正文跟着最新一项走 (番号/标题/海报), 但计时重新开始 → 一批任务最后合成一条.
            pending.variables = variables
            if pending.timer is not None:
                pending.timer.cancel()
        pending.count += 1
        number = str(variables.get("number") or "")
        if number and number not in pending.numbers:
            pending.numbers.append(number)
        pending.timer = asyncio.create_task(self._flush_later(index, channel, failed, pending.first_at))

    async def _flush_later(
        self, index: int, channel: NotifyChannelConfig, failed: bool, first_at: float
    ) -> None:
        """静默窗口结束就发; 期间每来一个新事件都会被 cancel 后重新计时."""
        key = (index, failed)
        wait = float(channel.throttle_seconds)
        if channel.max_wait_seconds > 0:
            # 流水线一直不停时也要有上限, 否则永远等不到"静默".
            wait = min(wait, max(0.0, float(channel.max_wait_seconds) - (time.monotonic() - first_at)))
        if wait > 0:
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                return
        pending = self._pending.get(key)
        if pending is None or pending.timer is not asyncio.current_task():
            # 期间又来了新事件: 交给新的计时器发送, 这里退出.
            return
        self._pending.pop(key, None)
        variables = dict(pending.variables)
        count = max(pending.count, 1)
        variables["count"] = count
        variables["numbers"] = "、".join(pending.numbers)
        variables["batch"] = _batch_line(count, pending.numbers)
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
        headers = dict(channel.headers)
        if body.lstrip().startswith("{") and not any(key.lower() == "content-type" for key in headers):
            # 不带 Content-Type 时对方会按表单解析 body, Telegram 会直接回
            # "Bad Request: message text is empty" (实测 400).
            headers["Content-Type"] = "application/json"
        try:
            response = await web.request(
                channel.method.value.upper(),
                url,
                headers=headers or None,
                data=body or None,
                timeout=channel.timeout,
                # 4xx 不抛异常: 要把对方的错误正文记进日志, 否则只看到一个 "HTTP 400".
                ok_statuses=frozenset(range(400, 500)),
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
        poster = ""
        thumb = ""
        plot = ""
        tags = ""
        actors = ""
        studio = ""
        release = ""
        category = str(infer_content_type(number)) if number else ""
        category_label = _CATEGORY_LABELS.get(category, category)
        if number and repo is not None:
            metadata = await repo.get_metadata_by_number(number)
            if metadata is not None:
                title = metadata.title or ""
                # 通知里带图 / 带详情: 单个字段优先, 没有就取候选列表第一项
                # (DMM 高清 CDN 这类地址可直接交给 Telegram 抓取).
                poster = metadata.poster_url or (metadata.poster_urls[0] if metadata.poster_urls else "")
                thumb = metadata.thumb_url or (metadata.thumb_urls[0] if metadata.thumb_urls else "")
                plot = metadata.plot or ""
                tags = "、".join(metadata.tags or [])
                actors = "、".join(_actor_name(item) for item in (metadata.actors or []))
                studio = metadata.studio or ""
                release = metadata.release or ""
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
        # 通知里显示本地时间更直观 (按容器 TZ); 库里存的是 UTC naive.
        finished_local = ""
        if task is not None and task.finished_at is not None:
            finished_local = _local_time(task.finished_at)
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
            "plot": plot,
            "tags": tags,
            "actors": actors,
            "studio": studio,
            "release": release,
            "category": category,
            "category_label": category_label,
            "library": library,
            "poster_url": poster,
            "thumb_url": thumb,
            "finished_at": finished,
            "finished_at_local": finished_local,
            "link": f"{config.panel_url.rstrip('/')}/tasks" if config.panel_url else "",
            "log_tail": await self._log_tail(raw_id, config.log_tail_lines) if config.log_tail_lines > 0 else "",
            "count": 1,
            "numbers": number,
            "batch": "",
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
            if media is not None and media.number:
                # 库里的番号字段最准: 整理时文件还叫原始名, 从路径反推会失败.
                return media.number
            if media is not None:
                parsed = parse_file_info(media.path).number
                if parsed:
                    return parsed
        return ""

    async def _log_tail(self, task_id: Any, lines: int) -> str:
        """任务日志最后 ``lines`` 行; 读不到返回空串."""
        if not isinstance(task_id, int) or self._log_dir is None:
            return ""
        path = self._log_dir / "tasks" / f"task-{task_id}" / "task.log"
        return await asyncio.to_thread(_read_tail, path, lines)


def _read_tail(path: Path, lines: int) -> str:
    """读取文件末尾 N 行; 读不到返回空串."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = text.splitlines()[-lines:]
    return "\n".join(tail)


__all__ = ["NotificationService"]
