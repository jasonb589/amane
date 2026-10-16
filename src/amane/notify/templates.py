"""通知模板的占位替换.

认 ``{name}`` 与 ``{{name}}`` 两种写法; 占位名必须是标识符, 因此 JSON 模板里的
``{"content": ...}`` 不会被误吃. 认不出的占位原样保留 —— 模板写错时会出现在通知正文里,
而不是被静默删掉.

同一变量三种取法:

- ``{title}``   原样
- ``{title_j}`` JSON 字符串内容 (转义引号与换行, 不含外层引号)
- ``{title_u}`` URL 编码 (进 query / 路径用)

``render(..., json_mode=True)`` 时字符串默认按 JSON 转义, 数字与布尔保持字面量 (不加引号),
所以 ``{"chat_id":"1","text":"{number}"}`` 可以直接写; 需要强制原样时用 ``{title_r}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

_PLACEHOLDER = re.compile(r"\{\{?([A-Za-z_][A-Za-z0-9_]*)\}\}?")
_MODE_SUFFIXES: dict[str, str] = {"_j": "json", "_u": "url", "_r": "raw"}


def json_escape(value: str) -> str:
    """JSON 字符串内容: 转义引号与换行, 不含外层引号."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def render(template: str, variables: Mapping[str, Any], *, json_mode: bool = False) -> str:
    """把 ``{变量}`` 替换成对应的值; 未定义的占位保持原样."""
    default_mode = "json" if json_mode else ""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
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
        if mode == "json":
            return json_escape(text)
        if mode == "url":
            return quote(text, safe="")
        return text

    return _PLACEHOLDER.sub(replace, template)
