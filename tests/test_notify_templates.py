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
    assert channel.events == ["task.completed", "task.failed"]
    assert channel.task_types == []
    assert channel.throttle_seconds == 0


def test_notify_channel_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="method"):
        NotifyConfig.model_validate({"channels": [{"url": "https://x", "method": "put"}]})
