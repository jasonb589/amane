"""通知模板渲染: 内置 ``{变量}`` 简写引擎 (不引入 Jinja2, 一种语法到底).

写法 (大括号一层两层都认, 空格随意):

* 取值: ``{number}`` / ``{{number}}`` / ``{{ number }}``
* 截断: ``{plot:300}`` 或 ``{plot[:300]}`` —— 只取前 N 个字符, 例如 Telegram 标题 1024 字上限
* 取值方式: ``{title_j}`` JSON 转义 · ``{title_u}`` URL 编码 · ``{title_r}`` 强制原样
* 过滤器写法 (与后缀等价, 兼容老模板): ``{{ title | j }}``

``render(..., json_mode=True)`` 时字符串默认按 JSON 转义, 数字与布尔不加引号 (拼 JSON body 用).
认不出的占位**原样保留**: 模板写错时正文里能看见, JSON 结构也不会被吃掉.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

#: 双花括号与单花括号分两趟替换: 否则 ``{"ok":{success}}`` 结尾那半个 ``}`` 会被双花括号规则吃掉.
_DOUBLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)([^{}]*)\}\}")
_SINGLE = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)([^{}]*)\}")

#: ``_j`` / ``_u`` / ``_r`` 后缀 与 ``| j`` / ``| u`` / ``| r`` 过滤器等价.
_MODE_SUFFIXES: dict[str, str] = {"_j": "json", "_u": "url", "_r": "raw"}
_FILTERS: dict[str, str] = {
    "j": "json",
    "json": "json",
    "u": "url",
    "url": "url",
    "r": "raw",
    "raw": "raw",
}


def json_escape(value: str) -> str:
    """JSON 字符串内容: 转义引号与换行, 不含外层引号."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _parse_tail(tail: str) -> tuple[str | None, int | None]:
    """解析占位符尾部: ``| j`` 过滤器 与 ``:N`` / ``[:N]`` 截断 (两者顺序随意)."""
    mode: str | None = None
    limit: int | None = None
    for chunk in tail.split("|"):
        piece = chunk.strip()
        if not piece:
            continue
        if piece.startswith("[") and piece.endswith("]"):
            # 兼容 Python 切片写法 ``[:300]``
            piece = piece[1:-1].strip()
        if piece.startswith(":"):
            digits = piece[1:].strip()
            if digits.isdigit():
                limit = int(digits)
            continue
        mode = _FILTERS.get(piece.lower(), mode)
    return mode, limit


def render(template: str, variables: Mapping[str, Any], *, json_mode: bool = False, html_mode: bool = False) -> str:
    """把模板里的占位符换成变量值; 认不出的占位原样保留.

    ``html_mode`` 时对每个变量值做 HTML 转义 (模板里自己写的 ``<b>`` 等不受影响),
    用 ``| r`` 可以对该处关掉转义.
    """
    default_mode = "json" if json_mode else ""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        mode, limit = _parse_tail(match.group(2))
        if mode is None:
            mode = default_mode
            for suffix, kind in _MODE_SUFFIXES.items():
                stripped = name[: -len(suffix)]
                if name.endswith(suffix) and stripped in variables:
                    name, mode = stripped, kind
                    break
        if name not in variables:
            return match.group(0)
        value = variables[name]
        if isinstance(value, bool):
            # 布尔先于 int 判断: bool 是 int 的子类, JSON 里要的是 true/false 而不是 1/0.
            return "true" if value else "false"
        if isinstance(value, int | float):
            return str(value)
        text = "" if value is None else str(value)
        if limit is not None:
            text = text[:limit]
        if mode == "json":
            return json_escape(text)
        if mode == "url":
            return quote(text, safe="")
        if html_mode and mode != "raw":
            return html.escape(text)
        return text

    return _SINGLE.sub(replace, _DOUBLE.sub(replace, template))
