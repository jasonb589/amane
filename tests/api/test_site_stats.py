from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from amane.api.support.site_stats import collect_site_stats

if TYPE_CHECKING:
    from amane.db.repository import Repository


@pytest.mark.asyncio
async def test_site_stats_keeps_unknown_failure_reasons_readable() -> None:
    repo = SimpleNamespace(
        list_tasks=AsyncMock(
            return_value=[
                SimpleNamespace(
                    finished_at=None,
                    result={
                        "outcomes": [
                            {"site": "example", "outcome": "failed", "reason": reason}
                            for reason in ("timeout", "legacy_reason", None)
                        ]
                    },
                )
            ]
        )
    )
    result = await collect_site_stats(cast("Repository", repo), limit=20)
    assert result.tasks == 1
    assert result.items[0].failed == 3
    assert {item.reason: item.count for item in result.items[0].reasons} == {None: 2, "timeout": 1}
