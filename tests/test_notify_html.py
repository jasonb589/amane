"""通知模板的 HTML 转义（Telegram parse_mode=HTML）。"""

from amane.notify.templates import render


def test_html_mode_escapes_variables_only() -> None:
    out = render("<b>{title}</b>", {"title": "A & B <script>"}, html_mode=True)
    assert out == "<b>A &amp; B &lt;script&gt;</b>"


def test_html_mode_can_be_disabled_per_placeholder() -> None:
    out = render("<b>{title|r}</b>", {"title": "A & B"}, html_mode=True)
    assert out == "<b>A & B</b>"


def test_plain_mode_does_not_escape() -> None:
    out = render("{title}", {"title": "A & B"})
    assert out == "A & B"


def test_html_mode_keeps_truncation() -> None:
    out = render("{plot[:5]}", {"plot": "1234567890"}, html_mode=True)
    assert out == "12345"
