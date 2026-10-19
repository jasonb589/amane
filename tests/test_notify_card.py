"""通知卡片: 空字段收尾 (``_squash_lines``) 与卡片用的精简变量."""

import json

from amane.config.manager import NotifyChannelConfig, NotifyProvider
from amane.notify.service import (
    _ellipsis,
    _short_list,
    _short_time,
    _squash_lines,
    build_request,
)


def _telegram(text: str = "") -> NotifyChannelConfig:
    return NotifyChannelConfig(
        provider=NotifyProvider.TELEGRAM,
        token="1:a",
        chat_id="-100123",
        parse_mode="HTML",
        text=text,
    )


def test_squash_drops_icon_only_lines() -> None:
    text = "✅ <b>刮削</b> · 成功\n👥 \n🖼 有码 · 💾 主库"
    assert _squash_lines(text) == "✅ <b>刮削</b> · 成功\n🖼 有码 · 💾 主库"


def test_squash_keeps_one_blank_line_between_sections() -> None:
    assert _squash_lines("a\n\n\n\nb\n\n\n") == "a\n\nb"


def test_squash_drops_empty_leading_field_but_keeps_next_icon() -> None:
    # 片商为空时 ``🏢  · 📅 2026-01-01`` 只剩表情和一个悬空分隔符: 去掉前面那段, 留下日期.
    assert _squash_lines("🏢  · 📅 2026-01-01") == "📅 2026-01-01"


def test_squash_strips_dangling_trailing_separator() -> None:
    assert _squash_lines("🏢 片商 · 📅 ") == "🏢 片商"


def test_squash_keeps_html_tags() -> None:
    assert _squash_lines("<b>标题</b>") == "<b>标题</b>"


def test_short_list() -> None:
    assert _short_list([], 3, "人") == ""
    assert _short_list(["a", "b"], 3, "人") == "a、b"
    assert _short_list(["a", "b", "c", "d"], 3, "人") == "a、b、c 等 4 人"


def test_ellipsis() -> None:
    assert _ellipsis("短", 160) == "短"
    assert _ellipsis("  a \n b  ", 160) == "a b"
    long_text = _ellipsis("x" * 200, 160)
    assert len(long_text) == 161
    assert long_text.endswith("…")


def test_short_time() -> None:
    assert _short_time("2026-10-18 04:00:39") == "10-18 04:00"
    assert _short_time("") == ""


def test_telegram_card_hides_empty_fields() -> None:
    channel = _telegram("✅ <b>{type_label}</b>\n👥 {actors_short}\n🏢 {studio} · 📅 {release}\n🖼 {category_label}")
    _, _, body = build_request(
        channel,
        {
            "type_label": "刮削",
            "actors_short": "",
            "studio": "",
            "release": "2026-01-01",
            "category_label": "有码",
        },
    )
    assert json.loads(body)["text"] == "✅ <b>刮削</b>\n📅 2026-01-01\n🖼 有码"


def test_default_text_uses_card_variables() -> None:
    _, _, body = build_request(
        _telegram(),
        {
            "type_label": "刮削",
            "status": "成功",
            "status_icon": "✅",
            "number": "ABC-1",
            # 其余字段全部缺省: 默认正文里那几行应当整行消失.
            "title": "",
            "actors_short": "",
            "studio": "",
            "release": "",
            "category_label": "",
            "library": "",
            "duration": "",
            "time_short": "",
            "batch": "",
        },
    )
    text = json.loads(body)["text"]
    # 没给的字段那几行整行消失, 不留空白行.
    assert text == "✅ 刮削 · 成功\n🎬 ABC-1"
