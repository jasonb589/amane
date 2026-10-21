"""通知: 任务完成 / 失败时按渠道模板推出去.

入口只有 ``on_event``: 由 ``EventBus`` 的监听器在 emit 之后调用, 因此**绝不能把异常抛回**
任务链路 (见 ``EventBus._notify_listeners``). 实际发送放在后台 task 里, 不阻塞 worker.

变量取自任务记录 (``repo.get_task``): 类型 / 状态 / 耗时 / 错误 / payload 里的番号与库,
再按需补标题 (``get_metadata_by_number``) 与任务日志尾部.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..config.manager import NotifyChannelConfig, NotifyConfig, NotifyProvider
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


def _batch_line(count: int, numbers: list[str], limit: int = 5) -> str:
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


#: HTML 标签不算"内容": 模板自己写的 ``<b>`` 不该把空字段那一行留在卡片上.
_TAG_RE = re.compile(r"<[^>]*>")
_WORD_RE = re.compile(r"\w")
#: 行尾悬空的字段: ``🏢 片商 · 📅 `` 里 ``·`` 后面那个空字段, 连分隔符一起去掉.
#: 只吃"分隔符之后没有任何字母数字汉字"的尾巴, ``有码 · 💾 主库`` 这种正常尾巴不动.
_TRAIL_JUNK_RE = re.compile(r"[\s·・|｜,，、]+[^\w<]*$")
#: 行首"图标 + 分隔符"但分隔符前没有任何内容的前缀 (``🏢  · 📅 2026-01-01`` 里的 ``🏢  · ``
#: 前面那个空字段): 连分隔符一起去掉, 后面那个字段的图标留着.
_LEAD_JUNK_RE = re.compile(r"^[^\w<]*(?:·|・|\|｜)\s*")


def _squash_lines(text: str) -> str:
    """卡片收尾: 丢掉只剩图标的空行, 合并连续空行, 去掉悬空的分隔符.

    模板里把可选字段 (出演者 / 标签 / 剧情) 各写成一行, 字段缺省时那一行只剩图标和 ``·``,
    原样发出去就是一串丑空行. 这里按"去掉 HTML 标签后还有没有字母数字汉字"来判定.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = _LEAD_JUNK_RE.sub("", raw.rstrip(), count=1)
        if not line:
            if lines and not lines[-1]:
                continue
            lines.append("")
            continue
        if not _WORD_RE.search(_TAG_RE.sub("", line)):
            continue
        lines.append(_TRAIL_JUNK_RE.sub("", line))
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


def _short_list(items: list[str], limit: int, unit: str) -> str:
    """卡片上的列表字段: 超过 ``limit`` 项收成"前 N 项 等 M 人"."""
    if not items:
        return ""
    if len(items) <= limit:
        return "、".join(items)
    return f"{'、'.join(items[:limit])} 等 {len(items)} {unit}"


def _ellipsis(text: str, limit: int) -> str:
    """长文本截断并补省略号 (模板的 ``:N`` 不带省略号, 卡片上断得很突兀)."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def _short_time(value: str) -> str:
    """``2026-10-18 04:00:39`` → ``10-18 04:00``: 卡片上时间不占宽度."""
    return value[5:16] if len(value) >= 16 else value


#: 供应商正文上限: 超了对方直接回 400 (telegram 文本 4096 / 图片说明 1024, 企业微信 2048, bark 正文 1000).
_MAX_TEXT: dict[NotifyProvider, int] = {
    NotifyProvider.TELEGRAM: 4096,
    NotifyProvider.WECOM: 2048,
    NotifyProvider.BARK: 1000,
}
_MAX_CAPTION = 1024
#: 5xx / 429 / 断连时的重试间隔.
_RETRY_DELAY_S = 2.0
_OPEN_TAG_RE = re.compile(r"<[^>]*$")


def _batch_summary(count: int, numbers: list[str], limit: int = 5) -> str:
    """合并成多条时的摘要行 (以换行开头; 单条时返回空串, 模板里那一段自然消失).

    ``count`` 是合并进这条消息的**任务事件数**, ``numbers`` 是其中解析出番号的去重列表:
    同一部片的多文件 (CD1/CD2 / 字幕版) 各自算一个事件, 自动整理还会再补一条子任务事件,
    所以两个数往往不相等. 文案里必须把"任务数"和"影片数"分开写, 否则就会像
    "共 80 项"却只涉及十几部片那样看不懂.
    """
    if count <= 1:
        return ""
    distinct = list(dict.fromkeys(numbers))
    if not distinct:
        return f"\n共 {count} 个任务"
    shown = "、".join(distinct[:limit])
    tail = " 等" if len(distinct) > limit else ""
    return f"\n共 {count} 个任务 · 涉及 {len(distinct)} 部：{shown}{tail}"


def _clip(text: str, limit: int) -> str:
    """按服务上限截断 (省略号算在限额内), 顺手砍掉被切断的半截 HTML 标签.

    截断点落在 ``<blockquote`` 中间时留下半个标签, Telegram 会直接报 entities 解析失败.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return _OPEN_TAG_RE.sub("", text[: limit - 1]).rstrip() + "…"


def _error_short(error: str, limit: int = 120) -> str:
    """失败通知里的错误摘要: 取第一段非空文本 (堆栈第一行), 按字数收一下."""
    for line in error.splitlines():
        text = line.strip()
        if text:
            return _ellipsis(text, limit)
    return ""


#: 预设服务的默认正文: text 留空时用它, 保证"选好服务就能收到东西".
#: 空字段 (缺出演者 / 片商) 那一行由 ``_squash_lines`` 收掉, 不会留下孤零零的图标.
_DEFAULT_TEXT = (
    "{status_icon} {type_label} · {status}\n"
    "🎬 {number}\n"
    "{title}\n"
    "👥 {actors_short}\n"
    "🏢 {studio} · 📅 {release}\n"
    "🖼 {category_label} · 💾 {library}\n"
    "🚫 {error_short}\n"
    "⏱ {duration} · 🕒 {time_short}{batch}"
)


def _split_title(text: str) -> tuple[str, str]:
    """Bark 这类需要标题+正文的服务: 第一行当标题, 其余当正文."""
    head, _, rest = text.partition("\n")
    return head.strip() or "Amane", rest.strip()


def build_request(channel: NotifyChannelConfig, variables: dict[str, Any]) -> tuple[str, dict[str, str], str]:
    """生成请求 (url, headers, body).

    ``provider = custom`` 用配置里的 url/body 原样渲染; 内置服务由程序拼 URL 与 JSON,
    用户只需要填 token / chat_id / 正文.
    """
    headers = dict(channel.headers)
    if channel.provider is not NotifyProvider.CUSTOM:
        html_mode = channel.provider is NotifyProvider.TELEGRAM and channel.parse_mode.strip().upper() == "HTML"
        rendered = _squash_lines(render(channel.text or _DEFAULT_TEXT, variables, html_mode=html_mode))
        headers["Content-Type"] = "application/json"
        if channel.provider is NotifyProvider.TELEGRAM:
            chat_id = channel.chat_id.strip()
            photo = str(variables.get("poster_url") or "") if channel.send_photo else ""
            if photo:
                method = "sendPhoto"
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "photo": photo,
                    "caption": _clip(rendered, _MAX_CAPTION),
                }
            else:
                method = "sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": _clip(rendered, _MAX_TEXT[NotifyProvider.TELEGRAM]),
                    "disable_web_page_preview": True,
                }
            if channel.parse_mode:
                payload["parse_mode"] = channel.parse_mode
            token = channel.token.strip()
            return f"https://api.telegram.org/bot{token}/{method}", headers, json.dumps(payload, ensure_ascii=False)
        if channel.provider is NotifyProvider.BARK:
            title, body = _split_title(_clip(rendered, _MAX_TEXT[NotifyProvider.BARK]))
            payload = {"device_key": channel.token.strip(), "title": title, "body": body}
        if channel.provider is NotifyProvider.WECOM:
            payload = {"msgtype": "text", "text": {"content": _clip(rendered, _MAX_TEXT[NotifyProvider.WECOM])}}
            key = channel.token.strip()
            return (
                f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
                headers,
                json.dumps(payload, ensure_ascii=False),
            )
    body = render(channel.body, variables, json_mode=channel.body.lstrip().startswith("{"))
    return render(channel.url, variables), headers, body


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
        self._sending: set[asyncio.Task[Any]] = set()

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
        if not channel.enabled:
            return False
        if channel.provider is NotifyProvider.CUSTOM:
            if not channel.url:
                return False
        elif not channel.token.strip():
            # 预设渠道的地址由程序拼, 缺的是 token; 这里记一条, 免得又变成"静默不匹配".
            logger.warning("notify channel skipped: token missing", channel=channel.name or str(channel.provider))
            return False
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

    async def _flush_later(self, index: int, channel: NotifyChannelConfig, failed: bool, first_at: float) -> None:
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
        distinct = list(dict.fromkeys(pending.numbers))
        variables["count"] = count
        variables["task_count"] = count
        variables["movie_count"] = len(distinct)
        variables["numbers"] = "、".join(distinct)
        variables["batch"] = _batch_summary(count, distinct)

        self._spawn(channel, variables)
    def _spawn(self, channel: NotifyChannelConfig, variables: dict[str, Any]) -> None:
        task = asyncio.create_task(self._send(channel, variables))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _send(self, channel: NotifyChannelConfig, variables: dict[str, Any]) -> tuple[bool, str]:
        web = self._web
        # enabled 已保证 web 非空; send_test 也会先判空, 这里只是兜底.
        if web is None:
            return False, "网络客户端未就绪"
        label = channel.name or channel.url or str(channel.provider)
        if channel.provider is not NotifyProvider.CUSTOM:
            if not channel.token.strip():
                logger.warning("notify channel skipped: token missing", channel=label)
                return False, "缺少 Token / Key"
            if channel.provider is NotifyProvider.TELEGRAM and not channel.chat_id.strip():
                logger.warning("notify channel skipped: chat_id missing", channel=label)
                return False, "缺少频道 / 群 ID"
        url, headers, body = build_request(channel, variables)
        if body.lstrip().startswith("{") and not any(key.lower() == "content-type" for key in headers):
            # 不带 Content-Type 时对方会按表单解析 body, Telegram 会直接回
            # "Bad Request: message text is empty" (实测 400).
            headers["Content-Type"] = "application/json"
        response = None
        last_error = ""
        # 对方 5xx / 429 / 断连时重试一次: 漏一条通知比多等两秒更麻烦.
        for attempt in (1, 2):
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
                response = None
                last_error = f"请求失败: {exc}"
                logger.warning("notify request failed", channel=label, error=str(exc), attempt=attempt)
            if response is not None and response.status_code != 429 and response.status_code < 500:
                break
            if attempt == 1:
                await asyncio.sleep(_RETRY_DELAY_S)
        if response is None:
            return False, last_error or "请求失败"
        if response.status_code >= 400:
            detail = response.text[:200]
            logger.warning(
                "notify rejected",
                channel=label,
                status=response.status_code,
                body=detail,
            )
            return False, f"HTTP {response.status_code}: {detail}"
        return True, f"HTTP {response.status_code}"
        return True, f"HTTP {response.status_code}"

    async def send_test(self, channel: NotifyChannelConfig) -> tuple[bool, str]:
        """用示例数据真实发送一次并返回结果, 供面板上的"测试"按钮调用."""
        if self._web is None:
            return False, "通知未启用或网络客户端未就绪"
        variables = dict(_SAMPLE_VARIABLES)
        config = self._config
        if config is not None and config.panel_url:
            variables["link"] = f"{config.panel_url.rstrip('/')}/tasks"
        return await self._send(channel, variables)

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
        series = ""
        directors = ""
        score = ""
        runtime = ""
        actor_list: list[str] = []
        tag_list: list[str] = []
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
                tag_list = [str(item) for item in (metadata.tags or []) if str(item).strip()]
                actor_list = [name for name in map(_actor_name, metadata.actors or []) if name]
                tags = "、".join(tag_list)
                actors = "、".join(actor_list)
                studio = metadata.studio or ""
                release = metadata.release or ""
                series = metadata.series or ""
                directors = "、".join(metadata.directors or [])
                score = f"{metadata.score:g}" if metadata.score is not None else ""
                runtime = f"{metadata.runtime} 分钟" if metadata.runtime else ""
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
        panel = config.panel_url.rstrip("/")
        link = f"{panel}/tasks" if panel else ""
        return {
            "task_id": raw_id if isinstance(raw_id, int) else "",
            "type": task_type,
            "type_label": _TYPE_LABELS.get(task_type, task_type),
            "status": "失败" if failed else "成功",
            "success": not failed,
            "status_icon": "❌" if failed else "✅",
            "error": error,
            # 失败卡片只放摘要: 完整堆栈留给日志页, 群里一条消息刷屏没人看.
            "error_short": _error_short(error),
            "duration": _format_duration(task.started_at, task.finished_at) if task is not None else "",
            "number": number,
            "title": title,
            "plot": plot,
            # 卡片用的精简版: 长字段按条数 / 字数收一收, 免得一条通知占满屏幕.
            "plot_short": _ellipsis(plot, 160),
            "tags": tags,
            "tags_short": _short_list(tag_list, 4, "个"),
            "actors": actors,
            "actors_short": _short_list(actor_list, 3, "人"),
            "studio": studio,
            "release": release,
            "year": release[:4] if release else "",
            "series": series,
            "directors": directors,
            "score": score,
            "runtime": runtime,
            "category": category,
            "category_label": category_label,
            "library": library,
            "poster_url": poster,
            "thumb_url": thumb,
            "finished_at": finished,
            "finished_at_local": finished_local,
            "time_short": _short_time(finished_local),
            "link": link,
            # 带面板按钮的整行 HTML: 用 ``{link_line|r}`` 引用 (关掉转义), 没有面板地址时整行为空.
            "link_line": f'<a href="{link}">▸ 打开面板</a>' if link else "",
            "log_tail": await self._log_tail(raw_id, config.log_tail_lines) if config.log_tail_lines > 0 else "",
            "count": 1,
            "task_count": 1,
            "movie_count": 1 if number else 0,
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


#: 测试发送用的示例数据: 让模板里的占位都能渲染出内容, 又不会去抓真实图片.
_SAMPLE_VARIABLES: dict[str, Any] = {
    "task_id": "",
    "type": "scrape",
    "type_label": "刮削",
    "status": "成功",
    "success": True,
    "error": "",
    "duration": "12.3s",
    "number": "ABC-123",
    "title": "测试标题 (来自通知测试)",
    "plot": "这是一条测试通知: 看到它说明该渠道的配置可用.",
    "plot_short": "这是一条测试通知: 看到它说明该渠道的配置可用.",
    "tags": "测试",
    "tags_short": "测试",
    "actors": "测试演员",
    "actors_short": "测试演员",
    "studio": "测试片商",
    "release": "2026-01-01",
    "year": "2026",
    "series": "测试系列",
    "directors": "测试导演",
    "score": "8.2",
    "runtime": "120 分钟",
    "category": "censored",
    "category_label": "有码",
    "library": "测试库",
    "poster_url": "",
    "thumb_url": "",
    "finished_at": "2026-01-01T12:00:00",
    "finished_at_local": "2026-01-01 20:00:00",
    "time_short": "01-01 20:00",
    "status_icon": "✅",
    "error_short": "",
    "count": 1,
    "task_count": 1,
    "movie_count": 1,
    "numbers": "",
    "batch": "",
    "link": "",
    "link_line": "",
    "log_tail": "",
}


__all__ = ["NotificationService"]
