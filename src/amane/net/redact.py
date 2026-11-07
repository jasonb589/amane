"""出站 HTTP 日志脱敏; 请求本身与普通爬虫录制内容保持原值."""

import json
import re
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote, quote_plus, unquote_plus, urlsplit, urlunsplit

from .recording import skip_body_recording

_PLACEHOLDER = "***"
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BOT_PATH = re.compile(r"(/bot)[^/]+", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"(?:^|[_-])(?:key|token|secret|password|signature|credential|auth)(?:$|[_-])", re.IGNORECASE
)
_HTTP_SECRETS: ContextVar[tuple[str, ...]] = ContextVar("http_secrets", default=())


@contextmanager
def sensitive_http(url: str, secrets: Iterable[str]) -> Generator[None]:
    """隐藏通知地址与已知凭据, 并停止保存可能回显凭据的响应正文."""
    values = set(_HTTP_SECRETS.get())
    for secret in (url, *secrets):
        if secret:
            values.update((secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]))
    token = _HTTP_SECRETS.set(tuple(sorted(values, key=len, reverse=True)))
    try:
        with skip_body_recording():
            yield
    finally:
        _HTTP_SECRETS.reset(token)


def redact_url(url: str) -> str:
    """隐藏 URL 中的凭据; 非敏感查询参数保留原始编码与顺序."""
    for secret in _HTTP_SECRETS.get():
        url = url.replace(secret, _PLACEHOLDER)
    if url == _PLACEHOLDER:
        return url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return _PLACEHOLDER
    netloc = parts.netloc.rsplit("@", 1)[-1]
    path = parts.path
    if host == "api.telegram.org":
        path = _BOT_PATH.sub(r"\1***", path)
    elif host == "api.day.app" and path not in ("", "/", "/push"):
        segments = path.split("/")
        if len(segments) > 1:
            segments[1] = _PLACEHOLDER
        path = "/".join(segments)
    query: list[str] = []
    for field in parts.query.split("&"):
        key, sep, value = field.partition("=")
        decoded = unquote_plus(key)
        sensitive = _QUERY_SECRET.search(decoded) or decoded.lower() in {"apikey", "accesskey", "authorization"}
        query.append(f"{key}{sep}{_PLACEHOLDER if sensitive and value else value}")
    return urlunsplit((parts.scheme, netloc, path, "&".join(query), parts.fragment))


def redact_text(text: str) -> str:
    """先替换完整凭据再处理 URL; 调用方仅可在脱敏后截断文本."""
    for secret in _HTTP_SECRETS.get():
        text = text.replace(secret, _PLACEHOLDER)
    return _URL.sub(lambda match: redact_url(match.group()), text)
