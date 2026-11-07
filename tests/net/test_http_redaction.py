"""敏感 HTTP 上下文与 URL 脱敏边界."""

import asyncio
from urllib.parse import quote

import pytest
import structlog

from amane.net.errors import RequestError
from amane.net.http import RateLimiters, WebClient
from amane.net.recording import skip_http_body
from amane.net.redact import redact_text, redact_url, sensitive_http
from tests.net.test_http import _StubResponse, _StubSession


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.telegram.org/bot123:private/sendMessage", "https://api.telegram.org/bot***/sendMessage"),
        (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=private",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=***",
        ),
        ("https://api.day.app/private/title/body", "https://api.day.app/***/title/body"),
        ("https://user:password@example.test/p?a=1&token=private", "https://example.test/p?a=1&token=***"),
        (
            "https://example.test/?api_key=private&tag=a%2Bb&tag=c+d",
            "https://example.test/?api_key=***&tag=a%2Bb&tag=c+d",
        ),
        ("https://example.test/?%74oken=private&count=3", "https://example.test/?%74oken=***&count=3"),
        (
            "https://example.test/a?search=a%2fb+z&empty=&keyless",
            "https://example.test/a?search=a%2fb+z&empty=&keyless",
        ),
        ("https://[invalid/private", "***"),
    ],
)
def test_redact_url_credentials_preserves_unrelated_query(url: str, expected: str) -> None:
    assert redact_url(url) == expected


def test_sensitive_context_masks_encoded_values_and_restores_after_error() -> None:
    url = "https://custom.test/opaque-secret-hook"
    secret = 'a&b/"c'
    text = f"{url} {secret} {quote(secret, safe='')}"
    with pytest.raises(ValueError), sensitive_http(url, [secret]):
        assert redact_text(text) == "*** *** ***"
        assert skip_http_body()
        with sensitive_http("https://nested.test/private", ["nested-secret"]):
            assert redact_text(f"{secret} nested-secret") == "*** ***"
        assert redact_text("nested-secret") == "nested-secret"
        raise ValueError("test cleanup")
    assert redact_text(secret) == secret
    assert not skip_http_body()


@pytest.mark.asyncio
async def test_sensitive_context_is_isolated_between_concurrent_tasks() -> None:
    entered = asyncio.Event()
    inspected = asyncio.Event()

    async def sensitive() -> None:
        with sensitive_http("https://custom.test/private", ["private-value"]):
            entered.set()
            await inspected.wait()
            assert redact_text("private-value") == "***"

    async def ordinary() -> None:
        await entered.wait()
        assert redact_text("private-value") == "private-value"
        assert not skip_http_body()
        inspected.set()

    await asyncio.gather(sensitive(), ordinary())


@pytest.mark.asyncio
async def test_http_failure_logs_hide_credentials_without_changing_request(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://custom.test/private-hook"
    client = WebClient(max_retries=0, limiters=RateLimiters(default_rate=100))
    session = _StubSession(_StubResponse(status=500))
    monkeypatch.setattr(client, "_session", session)
    with (
        structlog.testing.capture_logs() as records,
        sensitive_http(url, ["private-hook"]),
        pytest.raises(RequestError),
    ):
        await client.request("POST", url)
    assert session.calls[0]["url"] == url
    failed = [record for record in records if record["event"] == "request failed"]
    assert len(failed) == 1
    assert failed[0]["url"] == "***"
    assert "private-hook" not in str(records)
