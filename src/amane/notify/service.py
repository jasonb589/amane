"""通知: 任务完成 / 失败时按渠道模板推出去.

入口只有 ``on_event``: 由 ``EventBus`` 的监听器在 emit 之后调用, 因此**绝不能把异常抛回**
任务链路 (见 ``EventBus._notify_listeners``). 实际发送放在后台 task 里, 不阻塞 worker.

变量取自任务记录 (``repo.get_task``): 类型 / 状态 / 耗时 / 错误 / payload 里的番号与库,
再按需补标题 (``get_metadata_by_number``) 与任务日志尾部.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog

from ..config.manager import NotifyChannelConfig, NotifyConfig, NotifyProvider
from ..events import Event, EventType
from ..net.errors import RequestError
from ..net.redact import redact_text, sensitive_http
from ..parsing import infer_content_type, parse_file_info
from .telegram import clip_html, clip_text, plain_html
from .templates import render

if TYPE_CHECKING:
    from curl_cffi import Response

    from ..db.models import Task
    from ..db.repository import Repository
    from ..net.http import WebClient

logger = structlog.get_logger()

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
    "semantic_index": "语义索引",
    "semantic_benchmark": "语义基准测试",
    "purge": "清空回收站",
    "actor_push": "演员推送",
    "file_discovered": "文件发现",
    "file_removed": "文件移除",
}


#: 失败原因 → 通知里的中文说明 (``FailureReason`` 的取值).
_REASON_LABELS: dict[str, str] = {
    "no_usable_metadata": "没有匹配到条目",
    "not_listed": "站点没有这个番号",
    "not_found": "找不到条目",
    "http_error": "HTTP 错误",
    "api_error": "接口报错",
    "rate_limited": "被限流",
    "server_error": "服务端 5xx",
    "timeout": "超时",
    "network": "网络错误",
    "cloudflare_challenge": "Cloudflare 挑战",
    "cloudflare_blocked": "Cloudflare 拒绝",
    "ip_banned": "IP 被封",
    "geo_restricted": "地区限制",
    "age_verification": "年龄验证拦截",
    "login_required": "需要登录",
    "empty_response": "空响应",
    "parse_error": "解析失败",
    "crawler_unavailable": "来源不可用",
    "unexpected": "意外错误",
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
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone().strftime("%Y-%m-%d %H:%M:%S")


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
_LEAD_JUNK_RE = re.compile(r"^[^\w<]*(?:·|・|\||｜)\s*")


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
#: 合并消息里最多列多少条; 再多就折成「另 N 项」, 免得一条消息糊满屏.
_BATCH_ITEMS_LIMIT = 10

_MAX_TEXT: dict[NotifyProvider, int] = {
    NotifyProvider.TELEGRAM: 4096,
    NotifyProvider.WECOM: 2048,
    NotifyProvider.BARK: 1000,
}
_MAX_CAPTION = 1024
#: 5xx / 429 / 断连时的重试间隔.
_RETRY_DELAY_S = 2.0
_OPEN_TAG_RE = re.compile(r"<[^>]*$")


def _batch_summary(count: int, numbers: list[str], limit: int = 8) -> str:
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
    tail = " …" if len(distinct) > limit else ""
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
_DEFAULT_BATCH_TEXT = (
    "{status_icon} {type_label} · {status}\n"
    "{task_count} 个任务 · 涉及 {movie_count} 部\n"
    "💾 {library}\n\n{batch_items}\n\n🕒 {time_short}"
)


def _split_title(text: str) -> tuple[str, str]:
    """Bark 这类需要标题+正文的服务: 第一行当标题, 其余当正文."""
    head, _, rest = text.partition("\n")
    return head.strip() or "Amane", rest.strip()


def _batch_item(variables: dict[str, Any]) -> str:
    """合并窗口里的一项: 图标 + 番号 + 类型 (+ 缺哪些源 / 耗时). 供 ``{batch_items}`` 使用.

    只用纯文本: 通知正文按 HTML 模式渲染时变量值会被转义, 带上 ``<b>`` 就会原样显示成标签.
    """
    number = str(variables.get("number") or "").strip() or "—"
    icon = str(variables.get("status_icon") or "").strip()
    parts = [f"{icon} {number}".strip()]
    label = str(variables.get("type_label") or variables.get("type") or "").strip()
    if label:
        parts.append(label)
    sites = str(variables.get("failed_sites") or "").strip()
    if sites:
        parts.append(f"缺 {sites}")
    duration = str(variables.get("duration") or "").strip()
    if duration:
        parts.append(duration)
    return " · ".join(parts)


def _site_outcomes(task: Task | None) -> tuple[list[str], list[str], str, str]:
    """任务结果里的逐站点结果 → (失败站点, 失败明细行, amazon 状态, amazon 失败原因).

    面板只把明细放在任务详情里; 通知要让「哪里坏了」一眼可见, 所以这里整理成每站一行.
    """
    result = task.result if task is not None else None
    if not isinstance(result, dict):
        return [], [], "", ""
    outcomes = result.get("outcomes")
    if not isinstance(outcomes, list):
        return [], [], "", ""
    failed: list[str] = []
    lines: list[str] = []
    amazon = ""
    amazon_reason = ""
    for item in outcomes:
        if not isinstance(item, dict):
            continue
        site = str(item.get("site") or "")
        kind = str(item.get("outcome") or "")
        if site == "amazon" or site.startswith("amazon:"):
            amazon = "failed" if kind == "failed" else "ok"
            amazon_reason = str(item.get("reason") or "") if kind == "failed" else ""
        if kind != "failed":
            continue
        failed.append(site)
        reason = str(item.get("reason") or "")
        status = item.get("http_status")
        detail = str(item.get("detail") or "")
        extra = " / ".join(part for part in (f"HTTP {status}" if status else "", _ellipsis(detail, 80)) if part)
        lines.append(f"{site}: {_REASON_LABELS.get(reason, reason) or '失败'}" + (f" ({extra})" if extra else ""))
    return failed, lines, amazon, amazon_reason


_RESOURCE_URL_PREFIX = "/api/resources/"


def _render_text(channel: NotifyChannelConfig, variables: dict[str, Any]) -> str:
    """格式转义仅作用于变量; 无正文内容时仍保留用户输入的符号."""
    mode = channel.parse_mode if channel.provider is NotifyProvider.TELEGRAM else ""
    default = _DEFAULT_BATCH_TEXT if variables.get("count", 1) > 1 else _DEFAULT_TEXT
    template = (channel.text or default).replace("\\r\\n", "\n").replace("\\n", "\n")
    text = render(template, variables, html_mode=mode == "HTML", markdown_mode=mode == "MarkdownV2")
    return _squash_lines(text) or text.strip()


def _photo_url(variables: dict[str, Any], panel_url: str, image_token: str | None) -> str:
    """挑一个 Telegram 能直接抓的海报 URL.

    内部资源 (``/api/resources/...``) 需要面板 token, 用只读图片 token 拼成公网地址
    (``?token=...``, 只能读资源, 不等于 API token); 拼不出来就退到缩略图, 再不行就不带图.
    """
    for key in ("poster_url", "thumb_url"):
        raw = str(variables.get(key) or "").strip()
        if not raw:
            continue
        if raw.startswith(("http://", "https://")):
            return raw
        if raw.startswith(_RESOURCE_URL_PREFIX) and panel_url and image_token:
            parts = urlsplit(f"{panel_url.rstrip('/')}{raw}")
            query = [(key, value) for key, value in parse_qsl(parts.query) if key != "token"]
            return urlunsplit(parts._replace(query=urlencode([*query, ("token", image_token)])))
    return ""


def build_request(
    channel: NotifyChannelConfig,
    variables: dict[str, Any],
    *,
    panel_url: str = "",
    image_token: str | None = None,
) -> tuple[str, dict[str, str], str]:
    """生成请求 (url, headers, body).

    ``provider = custom`` 用配置里的 url/body 原样渲染; 内置服务由程序拼 URL 与 JSON,
    用户只需要填 token / chat_id / 正文.
    """
    headers = dict(channel.headers)
    if channel.provider is not NotifyProvider.CUSTOM:
        rendered = _render_text(channel, variables)
        headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
        headers["Content-Type"] = "application/json"
        if channel.provider is NotifyProvider.TELEGRAM:
            chat_id = channel.chat_id.strip()
            # 多任务摘要没有单一海报; 避免使用最后一条任务的图片表示整批结果.
            photo = (
                _photo_url(variables, panel_url, image_token)
                if channel.send_photo and variables.get("count", 1) == 1
                else ""
            )
            clip = clip_html if channel.parse_mode == "HTML" else clip_text
            payload: dict[str, Any]
            if photo:
                method = "sendPhoto"
                payload = {
                    "chat_id": chat_id,
                    "photo": photo,
                    "caption": clip(rendered, _MAX_CAPTION),
                }
            else:
                method = "sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": clip(rendered or "Amane", _MAX_TEXT[NotifyProvider.TELEGRAM]),
                    "disable_web_page_preview": True,
                }
            if channel.parse_mode:
                payload["parse_mode"] = channel.parse_mode
            if channel.message_thread_id:
                payload["message_thread_id"] = channel.message_thread_id
            if channel.disable_notification:
                payload["disable_notification"] = True
            link = str(variables.get("link") or "")
            if channel.show_link and link.startswith(("http://", "https://")):
                payload["reply_markup"] = {"inline_keyboard": [[{"text": "打开任务面板", "url": link}]]}
            token = channel.token.strip()
            return f"https://api.telegram.org/bot{token}/{method}", headers, json.dumps(payload, ensure_ascii=False)
        if channel.provider is NotifyProvider.BARK:
            title, body = _split_title(_clip(rendered, _MAX_TEXT[NotifyProvider.BARK]))
            payload = {"device_key": channel.token.strip(), "title": title, "body": body}
            return "https://api.day.app/push", headers, json.dumps(payload, ensure_ascii=False)
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
    numbers: dict[str, None] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    """合并窗口内每一条的摘要行; 合成一条消息时按顺序列出来."""
    count: int = 0
    first_at: float = 0.0
    timer: asyncio.Task[None] | None = None


def _reply(provider: NotifyProvider, response: Response) -> tuple[int, str, float]:
    """HTTP 成功仍须校验供应商业务结果; 错误正文仅保留诊断摘要."""
    status = response.status_code
    if provider is NotifyProvider.CUSTOM:
        return status, response.text if not 200 <= status < 300 else "", 0
    try:
        data = json.loads(response.text)
    except ValueError, TypeError:
        return status, "通知服务返回无效 JSON", 0
    if not isinstance(data, dict):
        return status, "通知服务返回无效结果", 0
    success = {
        NotifyProvider.TELEGRAM: data.get("ok") is True,
        NotifyProvider.BARK: data.get("code") == 200,
        NotifyProvider.WECOM: data.get("errcode") == 0,
    }.get(provider, False)
    if success and 200 <= status < 300:
        return status, "", 0
    if provider is NotifyProvider.TELEGRAM and isinstance(data.get("error_code"), int):
        status = data["error_code"]
    parameters = data.get("parameters")
    retry = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
    delay = float(retry) if isinstance(retry, int | float) and 0 < retry <= 86400 else 0
    detail = str(data.get("description") or data.get("errmsg") or data.get("message") or "通知服务拒绝请求")
    return status, detail, delay


class NotificationService:
    """按 ``notify`` 配置推送任务终态; 未启用时零开销."""

    def __init__(self) -> None:
        self._config: NotifyConfig | None = None
        self._web: WebClient | None = None
        self._repo: Repository | None = None
        self._log_dir: Path | None = None
        self._image_token: str | None = None
        self._pending: dict[tuple[int, bool, str, str], _Pending] = {}
        self._sending: set[asyncio.Task[Any]] = set()
        self._slots = asyncio.Semaphore(4)

    def configure(
        self,
        config: NotifyConfig,
        *,
        web_client: WebClient | None,
        repo: Repository | None,
        log_dir: Path | None,
        image_token: str | None = None,
    ) -> None:
        """渠道变更清除旧窗口; 已开始发送的请求继续使用其客户端与配置."""
        if self._config != config:
            for pending in self._pending.values():
                if pending.timer is not None:
                    pending.timer.cancel()
            self._pending.clear()
            self._config = config.model_copy(deep=True)
        self._web = web_client
        self._repo = repo
        self._log_dir = log_dir
        self._image_token = image_token

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
        """关闭合并窗口, 在关闭网络客户端前有界等待已提交的发送."""
        timers = []
        for pending in self._pending.values():
            if pending.timer is not None:
                pending.timer.cancel()
                timers.append(pending.timer)
        self._pending.clear()
        self._config = None
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        if self._sending:
            _, waiting = await asyncio.wait(self._sending, timeout=10)
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)

    # ------------------------------------------------------------------ 事件

    async def on_event(self, event: Event) -> None:
        """EventBus 监听器; 任何失败都只记日志, 不向任务链路抛."""
        if event.type not in _EVENT_KEYS or not self.enabled:
            return
        config = self._config
        assert config is not None
        try:
            variables = await self._variables(event, config)
        except Exception:
            logger.exception("notify context failed", event_type=str(event.type))
            return
        if self._config is not config:
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
        key = (index, failed, str(variables.get("type") or ""), str(variables.get("library") or ""))
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
        if number:
            pending.numbers[number] = None
        item = _batch_item(variables)
        if item and len(pending.items) < _BATCH_ITEMS_LIMIT:
            pending.items.append(item)
        pending.timer = asyncio.create_task(self._flush_later(key, channel, pending.first_at))

    async def _flush_later(
        self, key: tuple[int, bool, str, str], channel: NotifyChannelConfig, first_at: float
    ) -> None:
        """静默窗口结束就发; 期间每来一个新事件都会被 cancel 后重新计时."""
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
        distinct = list(pending.numbers)
        variables["count"] = count
        variables["task_count"] = count
        variables["movie_count"] = len(distinct)
        variables["numbers"] = "、".join(distinct)
        variables["batch"] = _batch_summary(count, distinct)
        variables["batch_items"] = "\n".join(pending.items[:_BATCH_ITEMS_LIMIT])
        if count > len(pending.items):
            variables["batch_items"] += f"\n… 另 {count - len(pending.items)} 项"

        self._spawn(channel, variables)

    def _spawn(self, channel: NotifyChannelConfig, variables: dict[str, Any]) -> None:
        task = asyncio.create_task(self._send_guarded(channel, variables))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _send_guarded(self, channel: NotifyChannelConfig, variables: dict[str, Any]) -> tuple[bool, str]:
        async with self._slots:
            try:
                return await self._send(channel, variables)
            except Exception as exc:
                # 异常可能包含 URL 中的密钥; 返回类型而不记录完整 traceback 或请求内容.
                logger.warning(
                    "notify send failed", channel=channel.name or str(channel.provider), error=type(exc).__name__
                )
                return False, f"发送失败: {type(exc).__name__}"

    async def _send(self, channel: NotifyChannelConfig, variables: dict[str, Any]) -> tuple[bool, str]:
        web = self._web
        # enabled 已保证 web 非空; send_test 也会先判空, 这里只是兜底.
        if web is None:
            return False, "网络客户端未就绪"
        label = channel.name or str(channel.provider)
        if channel.provider is not NotifyProvider.CUSTOM:
            if not channel.token.strip():
                logger.warning("notify channel skipped: token missing", channel=label)
                return False, "缺少 Token / Key"
            if channel.provider is NotifyProvider.TELEGRAM and not channel.chat_id.strip():
                logger.warning("notify channel skipped: chat_id missing", channel=label)
                return False, "缺少频道 / 群 ID"
        panel = (self._config.panel_url if self._config is not None else "").strip()
        url, headers, body = build_request(channel, variables, panel_url=panel, image_token=self._image_token)
        used_photo = channel.provider is NotifyProvider.TELEGRAM and url.endswith("/sendPhoto")
        if body.lstrip().startswith("{") and not any(key.lower() == "content-type" for key in headers):
            # 不带 Content-Type 时对方会按表单解析 body, Telegram 会直接回
            # "Bad Request: message text is empty" (实测 400).
            headers["Content-Type"] = "application/json"
        response, last_error = await self._post(channel, url, headers, body, web=web)
        status, detail, _ = _reply(channel.provider, response) if response is not None else (0, last_error, 0)
        if (
            channel.provider is NotifyProvider.TELEGRAM
            and status == 400
            and channel.parse_mode
            and "parse entities" in detail.lower()
        ):
            payload = json.loads(body)
            field_name = "caption" if used_photo else "text"
            rendered = _render_text(channel, variables)
            plain = (
                plain_html(rendered)
                if channel.parse_mode == "HTML"
                else re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!\\])", r"\1", rendered)
            )
            payload[field_name] = clip_text(plain or "Amane", _MAX_CAPTION if used_photo else 4096)
            payload.pop("parse_mode", None)
            body = json.dumps(payload, ensure_ascii=False)
            response, last_error = await self._post(channel, url, headers, body, web=web)
            status, detail, _ = _reply(channel.provider, response) if response is not None else (0, last_error, 0)
        photo_errors = ("photo", "image", "file", "wrong type", "webpage", "url content")
        if used_photo and status == 400 and any(word in detail.lower() for word in photo_errors):
            # 图被拒 (Telegram 抓不到图 / 尺寸 / 群组限制) 时退回纯文本: 卡片本身总要发出去.
            logger.warning(
                "notify photo rejected, retry as text",
                channel=label,
                status=response.status_code if response is not None else None,
            )
            payload = json.loads(body)
            payload["text"] = payload.pop("caption", "") or "Amane"
            payload.pop("photo", None)
            payload["disable_web_page_preview"] = True
            url = url.removesuffix("sendPhoto") + "sendMessage"
            response, last_error = await self._post(
                channel, url, headers, json.dumps(payload, ensure_ascii=False), web=web
            )
            status, detail, _ = _reply(channel.provider, response) if response is not None else (0, last_error, 0)
        if response is None:
            return False, last_error or "请求失败"
        if not 200 <= status < 300 or detail:
            detail = self._safe_error(channel, detail)
            logger.warning(
                "notify rejected",
                channel=label,
                status=status,
                body=detail,
            )
            return False, f"HTTP {status}: {detail}"
        return True, f"HTTP {response.status_code}"

    async def _post(
        self, channel: NotifyChannelConfig, url: str, headers: dict[str, str], body: str | bytes, *, web: WebClient
    ) -> tuple[Response | None, str]:
        """状态码可重试时最多重发一次; Telegram 限流遵守 retry_after, 传输失败不重复投递."""
        response = None
        last_error = ""
        for attempt in (1, 2):
            try:
                secrets = [channel.token, self._image_token or "", *channel.headers.values()]
                with sensitive_http(url, secrets):
                    response = await web.request(
                        "GET"
                        if channel.provider is NotifyProvider.CUSTOM and channel.method.value == "get"
                        else "POST",
                        url,
                        headers=headers or None,
                        data=body or None,
                        timeout=channel.timeout,
                        # 状态码由通知层统一处理, 防止叠加网络层的重试.
                        ok_statuses=frozenset(range(300, 600)),
                        max_attempts=1,
                        allow_redirects=False,
                    )
            except RequestError as exc:
                response = None
                last_error = f"请求失败: {self._safe_error(channel, exc.message)}"
                logger.warning("notify request failed", channel=channel.name or str(channel.provider), error=last_error)
                break
            status, _, delay = _reply(channel.provider, response)
            if status != 429 and status < 500:
                break
            if attempt == 1:
                if delay > 60:
                    break
                await asyncio.sleep(delay or _RETRY_DELAY_S)
        return response, last_error

    def _safe_error(self, channel: NotifyChannelConfig, text: str) -> str:
        with sensitive_http(channel.url, [channel.token.strip(), self._image_token or "", *channel.headers.values()]):
            return redact_text(text)[:200]

    async def send_test(self, channel: NotifyChannelConfig) -> tuple[bool, str]:
        """用示例数据真实发送一次并返回结果, 供面板上的"测试"按钮调用."""
        if self._web is None:
            return False, "通知未启用或网络客户端未就绪"
        variables = dict(_SAMPLE_VARIABLES)
        config = self._config
        if config is not None and config.panel_url:
            variables["link"] = f"{config.panel_url.rstrip('/')}/tasks"
            variables["link_line"] = (
                f'<a href="{html.escape(variables["link"], quote=True)}">▸ 打开任务面板 · 查看详情</a>'
            )
        return await self._send_guarded(channel, variables)

    # ------------------------------------------------------------------ 变量

    async def _variables(self, event: Event, config: NotifyConfig) -> dict[str, Any]:
        repo = self._repo
        raw_id = event.data.get("task_id")
        task = None
        if repo is not None and isinstance(raw_id, int):
            task = await repo.get_task(raw_id)
        payload: dict[str, Any] = dict(task.payload or {}) if task is not None else dict(event.data)
        file_event = event.type in {EventType.FILE_DISCOVERED, EventType.FILE_REMOVED}
        if file_event and isinstance(event.data.get("media_file_id"), int):
            payload["media_file_ids"] = [event.data["media_file_id"]]
        task_type = (
            str(task.type)
            if task is not None
            else _EVENT_KEYS[event.type]
            if file_event
            else str(event.data.get("type") or "")
        )
        number = await self._number(repo, payload)
        if (
            not number
            and repo is not None
            and task is not None
            and isinstance(task.root_task_id, int)
            and task.root_task_id != task.id
        ):
            # 自动整理这类子任务的 payload 只带 media_file_ids, 而整理后那些 id 已失效 (记录被重建),
            # 番号回退到同一条链的根任务 (刮削) 的 payload, 否则合并摘要里只剩"共 N 个任务".
            root = await repo.get_task(task.root_task_id)
            if root is not None:
                number = await self._number(repo, dict(root.payload or {}))
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
                category = str(metadata.content_type)
                category_label = _CATEGORY_LABELS.get(category, category)
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
        if library_id is None and repo is not None:
            ids = payload.get("media_file_ids")
            if isinstance(ids, list) and ids and isinstance(ids[0], int):
                media = await repo.get_media_file(ids[0])
                if media is not None:
                    library_id = media.library_id
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
        elif file_event:
            finished = event.timestamp
            finished_local = _local_time(datetime.fromisoformat(event.timestamp))
        panel = config.panel_url.rstrip("/")
        link = f"{panel}/tasks" if panel else ""
        failed_sites, site_failure_lines, amazon_state, amazon_reason = _site_outcomes(task)
        return {
            "task_id": raw_id if isinstance(raw_id, int) else "",
            "type": task_type,
            "type_label": _TYPE_LABELS.get(task_type, task_type),
            "status": "失败"
            if failed
            else "已发现"
            if event.type is EventType.FILE_DISCOVERED
            else "已移除"
            if file_event
            else "成功",
            "success": not failed,
            "status_icon": "❌" if failed else "✅",
            "error": error,
            # 失败卡片只放摘要: 完整堆栈留给日志页, 群里一条消息刷屏没人看.
            "error_short": _error_short(error),
            # 逐站点明细: 让「哪里坏了」在通知里直接可见, 不用翻任务详情.
            "failed_sites": "、".join(failed_sites),
            "site_failures": "\n".join(site_failure_lines),
            "amazon_state": amazon_state,
            "amazon_reason": amazon_reason,
            # 渠道用 `require` 过滤: `amazon_failed` 任意失败; `amazon_broken` 只在真故障时非空.
            "amazon_failed": "1" if amazon_state == "failed" else "",
            # 只有「站点正常应答但抓不到」才算故障; 站点本就没有这个番号 (not_listed) 不报警.
            "amazon_broken": "1" if amazon_state == "failed" and amazon_reason != "not_listed" else "",
            "amazon_missing": "1" if amazon_reason == "not_listed" else "",
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
            # 链接行单独一行, 用粗体标签提示去面板看详情 (模板里用 `{link_line|r}` 引用).
            "link_line": f'<a href="{html.escape(link, quote=True)}">▸ 打开任务面板 · 查看详情</a>' if link else "",
            "log_tail": await self._log_tail(raw_id, config.log_tail_lines) if config.log_tail_lines > 0 else "",
            "count": 1,
            "task_count": 1,
            "movie_count": 1 if number else 0,
            "numbers": number,
            "batch": "",
            "batch_items": "",
        }

    async def _number(self, repo: Repository | None, payload: dict[str, Any]) -> str:
        """番号取 payload 的 ``number``; 没有就从路径或关联媒体文件反推."""
        direct = payload.get("number")
        if isinstance(direct, str) and direct:
            return direct
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
        path = payload.get("path") or payload.get("file_path") or payload.get("scope_path")
        if isinstance(path, str) and path:
            return parse_file_info(path).number or ""
        return ""

    async def _log_tail(self, task_id: Any, lines: int) -> str:
        """任务日志最后 ``lines`` 行; 读不到返回空串."""
        if not isinstance(task_id, int) or self._log_dir is None:
            return ""
        path = self._log_dir / "tasks" / f"task-{task_id}" / "task.log"
        return await asyncio.to_thread(_read_tail, path, lines)


def _read_tail(path: Path, lines: int) -> str:
    """最多读取末尾 256 KiB; 避免为一条通知载入整个任务日志."""
    if lines <= 0:
        return ""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - 256 * 1024)
            stream.seek(start)
            raw = stream.read()
            if start:
                _, separator, remaining = raw.partition(b"\n")
                raw = remaining if separator else raw
    except OSError:
        return ""
    tail = raw.decode("utf-8", errors="replace").splitlines()[-lines:]
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
    "failed_sites": "",
    "site_failures": "",
    "amazon_state": "",
    "amazon_reason": "",
    "amazon_failed": "",
    "amazon_broken": "",
    "amazon_missing": "",
    "count": 1,
    "task_count": 1,
    "movie_count": 1,
    "numbers": "",
    "batch": "",
    "batch_items": "",
    "link": "",
    "link_line": "",
    "log_tail": "",
}


__all__ = ["NotificationService"]
