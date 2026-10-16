"""通知模板渲染: 支持 ``{变量}`` 简写, 也支持 Jinja2 (``{{ 变量 }}`` / ``{% if %}``).

两种写法自动切换: 模板里出现 ``{{`` 或 ``{%`` 就交给 Jinja2, 否则用内置的简写替换.
简写形式下 ``{name}`` / ``{{name}}`` 都认, 认不出的占位原样保留 (模板写错时能一眼看见,
JSON 结构也不会被吃掉).

简写形式的取值后缀: ``{title_j}`` JSON 转义 · ``{title_u}`` URL 编码 · ``{title_r}`` 强制原样;
``render(..., json_mode=True)`` 时字符串默认按 JSON 转义, 数字与布尔不加引号.

**Jinja2 形式不做自动转义**: 拼 JSON 时字符串值要自己加 ``|j`` 过滤器, 例如
``{"text":"{{ title | j }}"}}``; 纯文本模板直接 ``{{ title }}`` 即可. 常用过滤器还有
``| default('N/A')`` 与切片 ``{{ plot[:200] }}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

try:  # Jinja2 由上游依赖间接提供; 缺失时只禁用 ``{{ }}`` 写法, 不影响简写形式.
    from jinja2 import Environment, Undefined
except ImportError:  # pragma: no cover - 依赖存在时为 False
    Environment = None  # type: ignore[assignment]
    Undefined = None  # type: ignore[assignment]

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: ``{{var}}`` 写法单独一条: 与 ``{var}`` 分两趟替换, 否则 ``{"ok":{success}}`` 结尾的字面 ``}``
#: 会被当成双花括号的一部分吃掉.
_BRACED_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_MODE_SUFFIXES: dict[str, str] = {"_j": "json", "_u": "url", "_r": "raw"}


def json_escape(value: str) -> str:
    """JSON 字符串内容: 转义引号与换行, 不含外层引号."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _jinja_env() -> Any:
    """懒建 Jinja 环境: 不自动转义 (输出是 JSON / 纯文本, 不是 HTML), 变量缺失渲染成空串."""
    assert Environment is not None
    env = Environment(autoescape=False, undefined=Undefined, keep_trailing_newline=True)
    env.filters["j"] = json_escape
    env.filters["json"] = json_escape
    return env


_JINJA_ENV: Any = None


def _render_jinja(template: str, variables: Mapping[str, Any]) -> str | None:
    global _JINJA_ENV
    if Environment is None:
        return None
    if _JINJA_ENV is None:
        _JINJA_ENV = _jinja_env()
    try:
        return _JINJA_ENV.from_string(template).render(**variables)
    except Exception:  # Jinja 语法错误: 退回简写替换, 让占位原样留在正文里
        return None


def render(template: str, variables: Mapping[str, Any], *, json_mode: bool = False) -> str:
    """渲染模板: 含 ``{{`` / ``{%`` 走 Jinja2, 否则按 ``{变量}`` 简写替换."""
    if "{{" in template or "{%" in template:
        rendered = _render_jinja(template, variables)
        if rendered is not None:
            return rendered

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

    return _PLACEHOLDER.sub(replace, _BRACED_PLACEHOLDER.sub(replace, template))
