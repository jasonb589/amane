"""自定义通知功能的自检: 模板渲染与配置模型 (本 fork 新增, 与上游文件无重叠)."""

from __future__ import annotations

import json

import pytest

from amane.config.manager import HotSettings, NotifyConfig, NotifyMethod
from amane.notify.templates import json_escape, render

_VARIABLES = {"number": "JUR-517", "title": 'a "b"\nc', "count": 3, "success": True}


def test_render_plain() -> None:
    assert render("{number}", _VARIABLES) == "JUR-517"
    assert render("{{number}}", _VARIABLES) == "JUR-517"
    assert render('"{title}"', _VARIABLES) == '"a "b"\nc"'


def test_render_json_mode_escapes_strings_only() -> None:
    # JSON 模式: 字符串转义引号与换行, 数字与布尔保持字面量 (不加引号).
    inner = json.dumps('JUR-517 a "b"\nc', ensure_ascii=False)
    assert render('{"text":"{number} {title}"}', _VARIABLES, json_mode=True) == '{"text":' + inner + "}"
    assert render('{"count":{count},"ok":{success}}', _VARIABLES, json_mode=True) == '{"count":3,"ok":true}'


def test_render_explicit_modes() -> None:
    variables = {"title": "a b", "number": "X-1"}
    assert render("{title_u}", variables) == "a%20b"
    assert render('"{title_j}"', variables) == '"a b"'
    # 强制原样: JSON 模式下也不转义.
    assert render('"{title_r}"', variables, json_mode=True) == '"a b"'


def test_unknown_placeholders_survive() -> None:
    # 认不出的占位原样保留: 模板写错时正文里能看见, 且 JSON 结构不会被吃掉.
    assert render('{"a":1,"unknown":"{nope}"}', _VARIABLES) == '{"a":1,"unknown":"{nope}"}'
    assert render('{"msgtype":"text","text":{"content":"{number}"}}', _VARIABLES, json_mode=True) == (
        '{"msgtype":"text","text":{"content":"JUR-517"}}'
    )


def test_json_escape_keeps_unicode() -> None:
    assert json_escape("无码") == "无码"


def test_notify_config_defaults() -> None:
    assert HotSettings().notify.enabled is False
    assert HotSettings().notify.channels == []


def test_notify_channel_validation() -> None:
    config = NotifyConfig.model_validate(
        {
            "enabled": True,
            "panel_url": "https://amane.example.com/",
            "channels": [{"name": "telegram", "url": "https://api.telegram.org/botX/sendMessage", "method": "post"}],
        }
    )
    channel = config.channels[0]
    assert channel.method is NotifyMethod.POST
    assert [str(item) for item in channel.events] == ["completed", "failed"]
    assert channel.task_types == []
    assert channel.throttle_seconds == 0


def test_notify_channel_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="method"):
        NotifyConfig.model_validate({"channels": [{"url": "https://x", "method": "put"}]})


def test_notify_events_accept_legacy_dotted_values() -> None:
    """早期写法 task.completed 归一到 completed, 老配置不会校验失败."""
    config = NotifyConfig.model_validate({"channels": [{"url": "https://x", "events": ["task.completed"]}]})
    assert [str(item) for item in config.channels[0].events] == ["completed"]


def test_render_jinja_mode() -> None:
    """模板出现 ``{{`` / ``{%`` 时走 Jinja2: 支持条件、default 过滤器与切片."""
    pytest.importorskip("jinja2")  # jinja2 是间接依赖: 镜像里单独装 (见 Dockerfile), 测试环境可能没有
    variables = {"number": "ABC-001", "plot": "一二三四五六七八九十", "empty": ""}
    assert render("{{ number }}", variables) == "ABC-001"
    assert render("{% if number %}有{% else %}无{% endif %}", variables) == "有"
    assert render("{{ nothing | default('N/A') }}", variables) == "N/A"
    assert render("{{ plot[:3] }}", variables) == "一二三"
    # JSON 模板里字符串值要自己加 |j 过滤器 (Jinja 模式不自动转义)
    assert render('{"t":"{{ number | j }}"}', variables, json_mode=True) == '{"t":"ABC-001"}'
    assert render('{"t":"{{ nothing | j }}"}', variables, json_mode=True) == '{"t":""}'
    # 语法写错时退回简写引擎, 不抛异常
    assert render("{% if number %}", variables) == "{% if number %}"
