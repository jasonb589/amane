"""通知测试接口的配置校验与结果契约; 不发送外部消息."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from amane.notify.service import NotificationService

if TYPE_CHECKING:
    from httpx2 import AsyncClient


@pytest.mark.asyncio
async def test_notify_api_reports_result_and_hides_invalid_values(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = AsyncMock(return_value=(True, "HTTP 200"))
    monkeypatch.setattr(NotificationService, "send_test", sender)
    valid = {"provider": "telegram", "token": "123:private", "chat_id": "-1001", "message_thread_id": 42}
    response = await client.post("config/notify/test", json=valid)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "HTTP 200"}
    assert sender.await_args is not None
    assert sender.await_args.args[0].message_thread_id == 42

    sender.return_value = (False, "HTTP 403: bot blocked")
    response = await client.post("config/notify/test", json=valid)
    assert response.json() == {"ok": False, "message": "HTTP 403: bot blocked"}

    response = await client.post("config/notify/test", json={**valid, "parse_mode": "private-invalid-mode"})
    assert response.status_code == 422
    assert "parse_mode" in response.json()["detail"]
    assert "private" not in response.text
    assert sender.await_count == 2
